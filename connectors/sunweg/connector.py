"""connectors/sunweg/connector.py -- SunWegConnector: o outro lado do seam
CONN-04 para a WEG.

connectors/sunweg/client.py (06-03) so fala HTTP e devolve JSON BRUTO -- nao
ha normalizacao la. Este modulo pega esse JSON bruto e produz o DTO unico
cross-vendor (`connectors.base.NormalizedReading`), alem de descoberta de
device (D-06) com o fallback de device sintetico (D-07).

O conector NAO abre conexao propria de banco nem de HTTP -- ele orquestra um
`SunWegClient` injetado (construtor) e um cursor de banco injetado
(`discover_devices`, passado pelo CLI de ingestao, ja escrito na Fase 2/3).
Isso mantem o conector trivialmente testavel (nenhuma conta SunWEG ao vivo
nem Postgres necessarios para os testes em connector_test.py) e mantem o
controle de commit/rollback com o CLI. Este e o mesmo desenho de
connectors/goodwe/connector.py -- o teste real de que o seam multi-vendor da
Fase 2 aguenta um segundo fabricante sem mudar connectors/common/db.py nem
connectors/base.py.

Confianca de shape (ver notas internas do projeto, 06-SPIKE-FINDINGS.md,
VEREDITO A, secoes `## SERIE INTRADIARIA` e
`## MAPEAMENTO OBSERVADO -> NormalizedReading` -- lidas na integra antes
deste arquivo ser escrito):
  - `ts` vem do campo `x` de `getenergiadia.energias[]`: HIGH confidence na
    granularidade (96 pontos de 15 min, byte a byte observados ao vivo);
    MEDIUM confidence na semantica do timestamp -- o sufixo textual "GMT" e
    enganoso, o valor e muito provavelmente hora LOCAL NAIVE da usina
    (America/Cuiaba, UTC-4), nunca UTC de verdade (ver `_localize`).
  - `power_w` vem de `y` do mesmo array: HIGH confidence de que e POTENCIA
    instantanea em kW (nao energia incremental, apesar do nome do endpoint),
    confirmada por dois cross-checks independentes ao vivo contra
    `getmapa.usinas[].potencia` e `gettotalizadores.dados.potencia_ativa_total`
    no mesmo instante. Requer x1000 para W.
  - `energy_day_kwh`/`energy_total_kwh`: a fonte observada ao vivo
    (`gettotalizadores.dados.energia_gerada_hoje`/`energia_gerada_total`,
    strings com unidade embutida como "24.55 kWh"/"200.14 MWh") e um
    endpoint DIFERENTE do que `SunWegClient.fetch_readings` consulta
    (`getenergiadia`, que so devolve `{x, y}` por ponto). `normalize()`
    ainda declara os aliases candidatos por defensividade/forward-
    compatibilidade (mesmo padrao `_first_present` do GoodWe), mas com o
    shape atual de `raw` esses dois campos SEMPRE resolvem para `None` --
    isso e o comportamento correto ("campo ausente vira None", nunca 0),
    nao um bug: uma leitura futura precisaria de um plano que busque
    `gettotalizadores` e injete esses valores no dict bruto por ponto
    (mesmo padrao de prefixo `_gsd_` usado no Ramo B para o `day`, se algum
    dia for necessario), fora do escopo deste plano (06-04).
  - `status_raw`: `getmapa.usinas[].status` (e as 3 variantes ambiguas
    `status_usina`/`situacao`/`max_status`) tambem sao metadado POR USINA de
    um endpoint diferente (`getmapa`), nao por-ponto de `getenergiadia`.
    Pela mesma razao acima, `status_raw` tambem resolve para `None` no shape
    atual de `raw` -- os aliases sao declarados na ordem de confianca do
    spike (`status` primeiro: e o nome mais generico/proximo de
    `status_raw` entre os 4 candidatos, sem nenhuma documentacao publica que
    desempate os outros 3) para o dia em que outro plano injetar esse dado
    por ponto.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from connectors.base import NormalizedReading
from connectors.common.db import resolve_plant_device
from connectors.sunweg.client import SunWegClient

# Formato observado AO VIVO do timestamp `x` de getenergiadia.energias[]
# (06-SPIKE-FINDINGS.md ## SERIE INTRADIARIA): "Wed, 22 Jul 2026 09:30:00 GMT"
# -- formato HTTP-date / RFC 2822-like (%a, %d %b %Y %H:%M:%S GMT). O sufixo
# "GMT" e ENGANOSO: e hora LOCAL NAIVE da usina, nao UTC real (confianca
# MEDIUM -- argumento de plausibilidade fisica no spike, a confirmar ao vivo
# em 06-09) -- por isso o parse abaixo DESCARTA esse sufixo (strptime nao
# tenta interpretar "GMT" como offset, so casa o literal) e localiza
# explicitamente com ZoneInfo(plant_tz), nunca confiando no texto.
_SUNWEG_NAIVE_TS_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"

_UTC = ZoneInfo("UTC")


def _first_present(point: dict, *keys: str):
    """Devolve o primeiro valor nao-None encontrado em `point` dentre `keys`,
    ou None se nenhuma bater. Identico em intencao ao helper homonimo de
    connectors/goodwe/connector.py -- nunca inventa um default 0/vazio;
    ausencia fica None."""
    for key in keys:
        value = point.get(key)
        if value is not None:
            return value
    return None


def _to_float(value) -> float | None:
    """Converte um valor numerico-like para float, ou None se ausente/nao
    parseavel. Nunca inventa 0 -- "sem dado" e semanticamente diferente de
    "0 medidos" (ex.: um ponto futuro da curva intradiaria, que a API
    devolve como null).

    06-SPIKE-FINDINGS.md ## SERIE INTRADIARIA: os valores de `y` observados
    ao vivo vem como string com PONTO decimal ("18.48", "0.07"), sem virgula
    e sem unidade embutida no proprio valor -- `float()` direto basta; uma
    variante pt-BR (`_to_float_ptbr`, virgula decimal) NAO foi necessaria
    para este shape observado."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_status_raw(value) -> str | None:
    """Preserva o codigo/texto de status bruto do vendor tal como veio (como
    string) -- D-08: esta fase so preserva status_raw, nunca mapeia/
    interpreta codigos de status (isso e trabalho da Fase 4, ja escrito e
    agnostico de fabricante)."""
    if value is None:
        return None
    return str(value)


class SunWegConnector:
    """Implementa o Protocol `Connector` (connectors/base.py) para o portal
    SunWEG. Delega todo HTTP para o `SunWegClient` injetado (06-03) e toda
    escrita de banco para o cursor injetado + helpers de
    connectors/common/db.py (Fase 2) -- esta classe so orquestra e
    normaliza."""

    manufacturer = "weg"

    def __init__(self, client: SunWegClient) -> None:
        self._client = client

    # -- Connector Protocol: delegacao fina para o client injetado ----------

    def authenticate(self) -> None:
        self._client.authenticate()

    def list_plants(self) -> list[dict]:
        return self._client.list_plants()

    def list_devices(self, plant_ext_id: str) -> list[dict]:
        return self._client.list_devices(plant_ext_id)

    def fetch_readings(self, plant_ext_id: str, day: date) -> list[dict]:
        return self._client.fetch_readings(plant_ext_id, day)

    # -- Localizacao de fuso ---------------------------------------------

    @staticmethod
    def _localize(naive_str: str, plant_tz: str) -> datetime:
        """Faz parse de um timestamp naive-local do SunWEG (formato
        `_SUNWEG_NAIVE_TS_FORMAT`, sufixo "GMT" enganoso -- ver docstring de
        modulo) e localiza com o fuso IANA DA PROPRIA USINA, entao converte
        para UTC.

        NUNCA hardcoda um offset (nada de `timedelta(hours=3|4)`) -- sempre
        `zoneinfo.ZoneInfo(plant_tz)`, para que isto continue correto por
        usina (a usina piloto e America/Cuiaba, UTC-4; uma futura usina em
        America/Sao_Paulo seria UTC-3 -- hardcodar qualquer um dos dois
        corrompe silenciosamente o outro) e continue correto se a politica
        de DST do Brasil mudar de novo no futuro.

        Se algum dia uma fonte SunWEG passar a devolver um timestamp JA
        aware (com offset ou sufixo `Z` real), este metodo NAO deve ser
        reaplicado sobre ele -- reaplicar `ZoneInfo` num valor ja aware
        desloca a leitura duas vezes. O shape observado ao vivo (spike
        06-01/06-03) e sempre a string naive acima; isso e revisitado se um
        shape diferente aparecer.
        """
        naive = datetime.strptime(naive_str, _SUNWEG_NAIVE_TS_FORMAT)
        local = naive.replace(tzinfo=ZoneInfo(plant_tz))
        return local.astimezone(_UTC)

    # -- CONN-04: JSON bruto do SunWEG -> NormalizedReading[] -----------------

    def normalize(
        self,
        raw: list[dict],
        tenant_id: int,
        plant_id: int,
        device_id: int,
        plant_tz: str,
    ) -> list[NormalizedReading]:
        """Transforma os pontos brutos intradiarios
        (`SunWegClient.fetch_readings`'s `energias[]`, cada item `{"x": ...,
        "y": ...}`) no DTO compartilhado `NormalizedReading`.

        - `raw` vazio (limitacao conhecida do endpoint para dias passados,
          06-SPIKE-FINDINGS.md ## VEREDITO) -> devolve `[]` limpo, nunca
          levanta.
        - Cada ponto tem seu timestamp `x` localizado via `_localize`
          usando `plant_tz` (nunca um offset hardcoded). Um ponto sem `x`
          utilizavel e PULADO (nao aborta a janela inteira) -- mesmo padrao
          do GoodWe.
        - `y` (potencia instantanea em kW, string ponto-decimal ou `None`
          para pontos futuros ainda nao alcancados pelo relogio do
          servidor) e convertido para float e multiplicado por 1000 para
          preencher `power_w` (a coluna e W, o vendor reporta kW -- ver
          docstring de modulo e 06-SPIKE-FINDINGS.md ## SERIE INTRADIARIA).
          `y=None` vira `power_w=None` -- NUNCA um `0` inventado para um
          instante que o sol ainda nao alcancou.
        - `energy_day_kwh`/`energy_total_kwh`/`status_raw` sao extraidos
          defensivamente pelos aliases candidatos (ver docstring de
          modulo), mas com o shape atual de `raw` (so `{x, y}` por ponto)
          eles sempre resolvem `None` -- ausencia real na fonte usada por
          este metodo, nao um bug.
        - `status_raw` nunca e mapeado/interpretado (D-08 -- Fase 4).
        """
        readings: list[NormalizedReading] = []
        for point in raw or []:
            ts_raw = point.get("x")
            if not ts_raw:
                # Sem timestamp utilizavel neste ponto -- pula
                # defensivamente em vez de levantar; um ponto ruim nao pode
                # abortar a janela inteira.
                continue
            ts = self._localize(ts_raw, plant_tz)

            power_kw = _to_float(_first_present(point, "y"))
            power_w = power_kw * 1000 if power_kw is not None else None

            readings.append(
                NormalizedReading(
                    tenant_id=tenant_id,
                    plant_id=plant_id,
                    device_id=device_id,
                    manufacturer=self.manufacturer,
                    ts=ts,
                    power_w=power_w,
                    energy_day_kwh=_to_float(
                        _first_present(
                            point, "energy_day_kwh", "energia_gerada_hoje"
                        )
                    ),
                    energy_total_kwh=_to_float(
                        _first_present(
                            point, "energy_total_kwh", "energia_gerada_total"
                        )
                    ),
                    status_raw=_to_status_raw(
                        _first_present(
                            point, "status", "status_usina", "situacao", "max_status"
                        )
                    ),
                )
            )
        return readings

    # -- D-06/D-07: descoberta de device + fallback sintetico -----------------

    def discover_devices(
        self,
        cur,
        tenant_id: int,
        plant_id: int,
        external_plant_id: str,
    ) -> list[tuple[int, dict | None]]:
        """Resolve o(s) device_id(s) aos quais as leituras desta usina devem
        se ligar.

        Uma lista de inversores VAZIA e ESTADO NORMAL (nunca erro) -- e
        exatamente a situacao da usina piloto hoje no SunWEG (ver notas
        internas do projeto, 06-SPIKE-FINDINGS.md): nenhum endpoint de
        inversores/dispositivos por usina existe na API observada, e
        `connectors/sunweg/client.py::list_devices` ja devolve `[]`
        incondicionalmente por isso. `resolve_plant_device`
        (connectors/common/db.py, Fase 2) ja resolve esse caso para o
        device sintetico `PLANT:<external_plant_id>` (`device_kind='plant'`,
        D-06/D-07) -- este metodo NAO duplica essa logica: nao ha nenhum
        condicional local checando lista vazia aqui; se sentir vontade de
        escrever um, e sinal de que `connectors/common/db.py` ja resolve.

        Nota sobre a chave `sn`: `resolve_plant_device` usa `inverter["sn"]`
        como `external_device_id` para inversores REAIS (convencao do SEMS).
        A WEG hoje SEMPRE cai no device sintetico (lista vazia acima), entao
        nenhum remapeamento de chave e exercitado nem necessario neste
        plano -- se um endpoint de inversores da WEG for descoberto no
        futuro com outro nome de chave (ex. `numeroSerie`), o remapeamento
        deve ser feito AQUI (antes de delegar), nunca em
        `connectors/common/db.py` (camada compartilhada, ja provada em
        producao).

        `cur` e INJETADO pelo CLI de ingestao; este conector nunca abre
        conexao propria. Nunca devolve lista vazia -- `readings.device_id`
        e `NOT NULL`.
        """
        inverters = self.list_devices(external_plant_id)
        return resolve_plant_device(
            cur,
            tenant_id,
            plant_id,
            self.manufacturer,
            external_plant_id,
            inverters,
        )
