"""
SunWegClient -- cliente HTTP para a API do portal SunWEG (`api.sunweg.net/v2`).

So faz chamadas HTTP e devolve JSON BRUTO -- normalizacao (nomes de campo,
parse de numero, unidade, timezone) e trabalho de 06-04
(`connectors/sunweg/connector.py`), nao deste modulo. Endpoints, payloads e
headers abaixo espelham EXATAMENTE o que o spike descartavel
(`connectors/sunweg/spike.py`, plano 06-01) confirmou AO VIVO contra a conta
real da usina piloto -- ver notas internas do projeto (06-SPIKE-FINDINGS.md).
Onde o spike so observou as chaves de topo (nunca o formato interno de uma
lista), a extracao usa multiplas chaves candidatas e nunca levanta por
formato inesperado -- devolve `[]` (mesmo padrao de
`connectors/goodwe/client.py::SemsClient._extract_station_list`).

A lib `rokam/sunweg` (v3.1.0, out/2024) foi usada SO como referencia de
pesquisa (D-01 do plano 06-01) -- nunca e dependencia de runtime; ela usa
uma biblioteca HTTP sincrona sem timeout padrao, proibida pela tabela
"What NOT to Use" das notas internas do projeto, e seu payload de login
antecede os campos `aceito`/`captchaToken` confirmados ao vivo (ver
06-SPIKE-FINDINGS.md, secao CORRECAO).

SECURITY: NUNCA logar a senha, o valor do header X-Auth-Token-Update, nem o
token. Apenas status codes, chaves de topo e formas (shapes) de resposta
podem ser logados/levantados em excecao.
"""

from datetime import date

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

BASE_URL = "https://api.sunweg.net/v2"
LOGIN_PATH = "login/autenticacao"
AUTH_HEADER = "X-Auth-Token-Update"

# Origin/Referer que a SPA real (sun.weg.net) sempre envia no login -- sem
# eles o spike original recebia HTTP 500 para uma conta real (payload
# incompleto, ver 06-SPIKE-FINDINGS.md secao CORRECAO). Nao confirmado como
# necessario nos demais endpoints (getmapa/gettotalizadores/getenergiadia),
# mas mantido so no login, onde foi observado ao vivo.
SPA_ORIGIN = "https://sun.weg.net"
SPA_REFERER = "https://sun.weg.net/"

# Transport/HTTP apenas -- falha de negocio (200 com success=false, ou o
# envelope de erro {"erro": ..., "success": false} do login) NAO e retentada
# aqui, levanta na hora. Identico a SemsClient (connectors/goodwe/client.py).
_RETRYABLE_EXC = (httpx.TransportError, httpx.HTTPStatusError)


class SunWegLoginError(Exception):
    """Login recusado pelo SunWEG (success=false, ou o corpo trouxe um `erro`
    conhecido: "invalido", "bloqueado", "bloqueado-inadinplecia", "recaptcha
    invalido"), ou a resposta veio sem token. NAO e retentado -- credencial
    errada ou bloqueio de conta nao melhora com backoff. O chamador
    (ingest/run.py) isola o grupo de credencial e segue (D-03 da Fase 03.1)."""


class SunWegApiError(Exception):
    """Resposta 200 com success=false num endpoint de dados, ou payload em
    formato inesperado. Carrega apenas o campo de mensagem do vendor (`erro`
    ou `message`) -- nunca o corpo inteiro (pode conter token)."""


class SunWegClient:
    """Cliente HTTP fino e especifico do vendor SunWEG. Devolve apenas JSON
    bruto -- normalizacao para o DTO compartilhado (ver `connectors/base.py`)
    acontece em `connectors/sunweg/connector.py` (06-04), nao aqui."""

    manufacturer = "weg"

    def __init__(self, account: str, password: str, timeout: float = 15.0) -> None:
        # timeout=15.0 (nao 10.0 como o SemsClient): o SunWEG e um portal
        # brasileiro menos exercitado que o SEMS; 15s foi o valor usado pelo
        # spike (06-01) e observado como confortavel ao vivo, sem timeouts.
        self._account = account
        self._password = password
        self._http = httpx.Client(timeout=timeout)
        self.token: str | None = None

    # -- autenticacao ---------------------------------------------------

    @retry(
        reraise=True,
        retry=retry_if_exception_type(_RETRYABLE_EXC),
        wait=wait_exponential_jitter(initial=1, max=8),
        stop=stop_after_attempt(4),
    )
    def _login_post(self, aceito: bool) -> dict:
        """POST cru no login -- payload de 5 campos confirmado ao vivo pelo
        spike 06-01 (06-SPIKE-FINDINGS.md, secao `## LOGIN FRIO / HEADLESS`):
        `{usuario, senha, rememberMe, aceito, captchaToken}`. Um payload de
        so 3 campos (sem `aceito`/`captchaToken`) produzia HTTP 500 para uma
        conta REAL -- nao usar a forma antiga documentada em pesquisas/planos
        anteriores a essa correcao. `captchaToken=""` foi ACEITO ao vivo (o
        Turnstile esta montado no front mas a validacao nao e aplicada no
        backend hoje -- ver OBSERVACOES/QUIRKS do spike; se isso mudar
        (`erro: "recaptcha invalido"`), o correto e levantar, nunca tentar
        resolver/contornar o captcha)."""
        resp = self._http.post(
            f"{BASE_URL}/{LOGIN_PATH}",
            json={
                "usuario": self._account,
                "senha": self._password,
                "rememberMe": False,
                "aceito": aceito,
                "captchaToken": "",
            },
            headers={
                "Content-Type": "application/json",
                "Origin": SPA_ORIGIN,
                "Referer": SPA_REFERER,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def authenticate(self) -> None:
        """Autentica contra o SunWEG e guarda o token so em memoria.

        D-02 (travada em 06-03-PLAN.md): chamado UMA vez por run, sem cache
        de token entre execucoes. O JWT do SunWEG dura 168h, mas nao e
        persistido em lugar nenhum (nem Postgres, nem cache do Actions):
        superficie de segredo em repouso que hoje nao existe e nao sera
        criada. Custo aceito: ~96 logins/dia; gatilho de revisao = rate
        limit / bloqueio observado ao vivo.
        """
        data = self._login_post(aceito=False)

        # Fluxo de "aceitar termos" (06-SPIKE-FINDINGS.md OBSERVACOES/QUIRKS):
        # se o backend responder erro="aceitar_termos", a SPA reenvia o
        # mesmo POST com aceito=true. Nao foi exercitado ao vivo (a conta da
        # usina piloto ja tinha os termos aceitos), mas tratado aqui como um
        # retry unico e conhecido -- nunca silenciosamente ignorado.
        if data.get("erro") == "aceitar_termos":
            data = self._login_post(aceito=True)

        if data.get("success") is not True:
            # O envelope de erro observado ao vivo usa a chave `erro`, NAO
            # `message` (06-SPIKE-FINDINGS.md: "invalido"/"bloqueado"/
            # "bloqueado-inadinplecia"/"recaptcha invalido"). `message` e
            # mantido como fallback defensivo caso outro erro use essa
            # chave -- nunca logamos o dict inteiro (pode ter token/usuario).
            reason = data.get("erro") or data.get("message") or "motivo desconhecido"
            raise SunWegLoginError(f"SunWEG login falhou: {reason}")

        # Chave de onde o token saiu, confirmada ao vivo: 'token' (JWT,
        # 06-SPIKE-FINDINGS.md). Envelope de sucesso observado:
        # {'success': True, 'token': str, 'usuario': {...}} -- sem 'message'.
        token = data.get("token")
        if not token:
            raise SunWegLoginError("SunWEG login sem token na resposta")
        self.token = token

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", AUTH_HEADER: self.token}

    @retry(
        reraise=True,
        retry=retry_if_exception_type(_RETRYABLE_EXC),
        wait=wait_exponential_jitter(initial=1, max=8),
        stop=stop_after_attempt(4),
    )
    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """Chamada autenticada de baixo nivel. Retentada via tenacity so em
        erro de transporte/HTTP -- nunca um loop manual de retry."""
        url = f"{BASE_URL}/{path.lstrip('/')}"
        resp = self._http.request(
            method, url, headers=self._headers(), params=params, json=json_body
        )
        resp.raise_for_status()
        return resp.json()

    def _authed(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """Garante autenticacao (chama `authenticate()` se ainda nao logado),
        faz a chamada, e faz UM UNICO relogin reativo em 401 (mesmo padrao de
        `SemsClient._authed_post`): o SunWEG usa 401 para token invalido/
        expirado (a lib de referencia mapeia isso para `LoginError`); com
        D-02 relogando a cada run isso quase nunca deve disparar -- se
        disparar com frequencia, e sinal de que o token esta sendo
        invalidado no servidor, e vira o "gatilho de revisao" de D-02.
        Depois da resposta, levanta `SunWegApiError` se o campo `success`
        EXISTIR e for False (alguns endpoints nao trazem `success` -- nunca
        levantar so pela ausencia do campo)."""
        if self.token is None:
            self.authenticate()

        try:
            data = self._request(method, path, params, json_body)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self.authenticate()
                data = self._request(method, path, params, json_body)
            else:
                raise

        if isinstance(data, dict) and data.get("success") is False:
            reason = data.get("message") or data.get("erro") or "motivo desconhecido"
            raise SunWegApiError(f"SunWEG {path}: {reason}")

        return data

    # -- listagem de usinas / dispositivos / leituras --------------------

    def list_plants(self) -> list[dict]:
        """Usinas cruas da conta, via `getmapa` (GET).

        06-SPIKE-FINDINGS.md ## ENDPOINTS: `getstatususinas.usinas` veio
        SEMPRE vazio ao vivo (mesmo passando `plantid` explicito) -- parece
        ser um painel de contagens/percentuais globais da conta, nao a lista
        de usinas. `getmapa.usinas[]` foi a fonte real confirmada, com 1
        item para a usina piloto (`id=147493`, nome interno da usina omitido
        aqui, `capacidade=103.5` [nameplate kWp], `potencia=18.48` [potencia
        instantanea kW no momento da chamada -- NAO confundir os dois]).
        Nenhum campo e renomeado ou convertido aqui -- normalizacao e de
        06-04."""
        data = self._authed("GET", "getmapa")
        return self._extract_plant_list(data)

    @staticmethod
    def _extract_plant_list(data) -> list[dict]:
        """Extracao defensiva no molde de
        `SemsClient._extract_station_list`: `getmapa` devolveu a lista sob a
        chave `usinas` ao vivo (06-SPIKE-FINDINGS.md), mas tambem aceitamos
        uma lista nua no topo ou outras chaves plausiveis, para nao quebrar
        se o formato mudar sutilmente. Nunca levanta -- `[]` se nada bater."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("usinas", "list", "data"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    return candidate
        return []

    def list_devices(self, plant_ext_id: str) -> list[dict]:
        """SEMPRE devolve `[]` -- ESTADO NORMAL, nunca uma falha.

        06-SPIKE-FINDINGS.md nao encontrou nenhum endpoint de
        inversores/dispositivos por usina na API SunWEG: os 4 endpoints
        confirmados (`getmapa`, `gettotalizadores`, `getenergiadia`,
        `getstatususinas`) nao listam inversores; `getdadosresumo` (o unico
        candidato restante) devolveu HTTP 500 em GET e 405 em POST (tentado
        explicitamente nesta task) e foi descartado -- ver 06-SPIKE-FINDINGS.md.
        Uma lista vazia aqui aciona o device sintetico
        `PLANT:<external_plant_id>` de
        `connectors/common/db.py::resolve_plant_device` (D-06/D-07 da Fase
        2, ja generico) -- a mesma situacao que a usina piloto ja vive no
        SEMS (Goodwe). `plant_ext_id` e aceito por compatibilidade de
        assinatura com o `Connector` Protocol, mas nao e usado."""
        del plant_ext_id  # nao usado -- ver docstring
        return []

    def fetch_readings(self, plant_ext_id: str, day: date) -> list[dict]:
        """Pontos crus da curva de potencia intradiaria via `getenergiadia`
        (GET), com `date=day.isoformat()`.

        06-SPIKE-FINDINGS.md VEREDITO A (confirmado ao vivo): 96 pontos (15
        em 15 min) para o dia corrente, cada item
        `{'x': <timestamp cru string>, 'y': <string decimal-ponto ou None>}`.
        `y` e POTENCIA instantanea em kW no instante `x` (nao energia
        incremental, apesar do nome do endpoint -- confirmado cruzando com
        `getmapa.potencia` e `gettotalizadores.dados.potencia_ativa_total`
        no mesmo instante). Pontos futuros (ainda nao alcancados pelo
        relogio do servidor) vem `y=None`.

        NAO faz parse de numero, NAO trata a virgula/ponto decimal, NAO
        trata o sufixo `GMT` enganoso de `x` (muito provavelmente hora local
        naive de America/Cuiaba, confianca MEDIA -- ver 06-SPIKE-FINDINGS.md
        ## SERIE INTRADIARIA) -- tudo isso e trabalho de 06-04
        (`normalize()`). O cliente so devolve o JSON bruto, ponto por ponto.

        LIMITACAO CONHECIDA (06-SPIKE-FINDINGS.md ## VEREDITO, nuance): este
        endpoint so serve o dia CORRENTE do servidor -- pedir uma data
        passada devolveu `energias: []` vazio, com `200 OK` (nao erro). NAO
        ha backfill historico via este endpoint; para um `day` diferente de
        hoje, `[]` pode significar "sem dado disponivel por limitacao da
        API", nao necessariamente "usina nao gerou" -- o contrato "vazio e
        normal, nunca excecao" continua valendo aqui, mas quem chama este
        metodo para um dia passado precisa saber dessa limitacao (registrada
        como risco de fase em aberto, sem plano dono ainda).

        `plant_ext_id`: o spike so testou uma conta com 1 usina e NUNCA
        precisou passar um identificador de usina para este endpoint (a
        curva veio escopada pela sessao/token, sem nenhum parametro de
        usina). Ainda assim, enviamos `plant_ext_id` como parametro
        `plantid` defensivamente (mesmo nome tentado, sem sucesso
        confirmado, em `getstatususinas`) para o caso de uma conta futura
        com mais de uma usina -- NAO CONFIRMADO ao vivo; se uma conta
        multi-usina passar por aqui devolvendo dados da usina errada, este e
        o primeiro lugar a revisar."""
        response = self._authed(
            "GET",
            "getenergiadia",
            params={"date": day.isoformat(), "plantid": plant_ext_id},
        )
        energias = response.get("energias", []) if isinstance(response, dict) else []
        return energias if isinstance(energias, list) else []
