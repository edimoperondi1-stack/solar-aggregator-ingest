"""notifiers/whatsapp/client.py -- cliente HTTP da WhatsApp Cloud API (Meta).

So faz chamada HTTP e devolve JSON bruto -- a formatacao da mensagem a partir
de um `AlarmEvent` e trabalho de `notifiers/whatsapp/notifier.py`, nao daqui
(mesma separacao cliente/normalizacao de `connectors/goodwe/client.py`).

API oficial da Meta (`graph.facebook.com`), escolhida em vez de gateway nao
oficial: nao ha risco de banimento do numero e nao ha intermediario pago. O
preco disso e a regra de JANELA DE 24 HORAS da Meta -- uma mensagem iniciada
pelo NEGOCIO (que e exatamente o nosso caso: alarme dispara sem ninguem ter
falado com o bot) so e entregue como TEMPLATE previamente aprovado. Texto
livre so funciona nas 24h seguintes a uma mensagem do proprio destinatario,
util apenas para teste manual -- por isso o modo default e `template` e
`send_text` existe so para o `--test` de `ingest/notify.py`.

SECURITY: NUNCA logar/levantar o token (`Authorization: Bearer ...`) nem o
telefone completo -- os logs de Actions deste repo sao PUBLICOS. Telefone
sempre via `mask_phone()`; o corpo de erro da Meta e repassado apenas no
campo `message`/`code`, nunca o payload enviado (que contem o numero).
"""

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from notifiers.base import mask_phone, normalize_phone

# Versao da Graph API fixada explicitamente: a Meta descontinua versoes com
# ~2 anos de aviso, e um default flutuante mudaria o formato de resposta sem
# nenhuma mudanca no nosso codigo. Subir a versao e uma decisao consciente.
GRAPH_API_VERSION = "v21.0"
GRAPH_BASE_URL = "https://graph.facebook.com"


class WhatsAppError(Exception):
    """Falha de negocio da Cloud API (4xx com envelope `error`, ou 200 sem
    `messages`). NAO e retentada -- numero invalido, template inexistente ou
    token expirado nao melhoram com backoff. Carrega so `message`/`code` da
    Meta e o telefone MASCARADO."""


def _is_retryable(exc: BaseException) -> bool:
    """Retenta erro de transporte, 429 (rate limit) e 5xx -- nunca os demais
    4xx.

    Divergencia DELIBERADA de `connectors/*/client.py`, que retentam qualquer
    `HTTPStatusError`: la o vendor devolve 5xx transitorio com frequencia; aqui
    um 400 significa "numero invalido"/"template nao existe"/"token expirado",
    que 4 tentativas so transformam em 4x o mesmo erro -- e, sob um cron de 15
    min, em atraso do alerta seguinte.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_retryable_status(exc.response.status_code)
    return False


def _is_retryable_status(status: int) -> bool:
    """Unica fonte de verdade de "status transitorio" -- usada tanto pelo
    predicado da tenacity quanto pelo `_post` ao decidir entre deixar
    retentar e levantar WhatsAppError na hora."""
    return status == 429 or 500 <= status < 600


class WhatsAppClient:
    """Cliente fino da Cloud API para UM numero remetente (phone_number_id)."""

    channel = "whatsapp"

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        api_version: str = GRAPH_API_VERSION,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`transport` existe SO para teste (httpx.MockTransport) -- em
        producao fica None e o httpx usa a pilha real. E o unico jeito de
        exercitar payload/retry/erro sem rede nem credencial de verdade."""
        self._token = token
        self._phone_number_id = phone_number_id
        self._url = (
            f"{GRAPH_BASE_URL}/{api_version}/{phone_number_id}/messages"
        )
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @retry(
        reraise=True,
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential_jitter(initial=1, max=8),
        stop=stop_after_attempt(4),
    )
    def _post(self, payload: dict, to: str) -> dict:
        """POST autenticado + validacao do envelope. `to` chega separado so
        para poder aparecer MASCARADO nas mensagens de erro."""
        resp = self._http.post(self._url, headers=self._headers(), json=payload)

        # Envelope de erro da Meta: 4xx/5xx com corpo {"error": {...}}. Lemos o
        # corpo ANTES do raise_for_status para poder citar message/code sem
        # despejar o payload (que contem o telefone) no log.
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            if _is_retryable_status(resp.status_code):
                # Deixa a tenacity retentar: 429/5xx sao transitorios.
                resp.raise_for_status()
            raise WhatsAppError(
                f"WhatsApp recusou envio para {mask_phone(to)}: {detail}"
            )

        data = resp.json()
        # 200 sem `messages` nao e sucesso -- a Meta devolve a lista de
        # mensagens aceitas; sem ela nada foi enfileirado.
        if not data.get("messages"):
            raise WhatsAppError(
                f"WhatsApp respondeu 200 sem `messages` para {mask_phone(to)}"
            )
        return data

    def send_text(self, to: str, body: str) -> dict:
        """Mensagem de texto livre. So entregue dentro da janela de 24h (ver
        nota no topo) -- use para teste manual, nao para alarme automatico."""
        to = normalize_phone(to)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            # preview_url=False: nao queremos que a Meta busque/expanda link
            # nenhum a partir do texto do alarme.
            "text": {"preview_url": False, "body": body},
        }
        return self._post(payload, to)

    def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        body_params: list[str],
    ) -> dict:
        """Mensagem de template aprovado -- o caminho de PRODUCAO (mensagem
        iniciada pelo negocio). `body_params` preenche os {{1}}..{{N}} do
        corpo do template, na ordem (ver `AlarmEvent.template_params`)."""
        to = normalize_phone(to)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": p} for p in body_params
                        ],
                    }
                ],
            },
        }
        return self._post(payload, to)


def _error_detail(resp: httpx.Response) -> str:
    """Extrai `error.message` / `error.code` da resposta, sem nunca devolver o
    corpo inteiro (pode ecoar o payload, e com ele o telefone)."""
    try:
        error = resp.json().get("error", {})
    except Exception:
        return f"HTTP {resp.status_code} (corpo nao-JSON)"
    message = error.get("message") or "motivo desconhecido"
    code = error.get("code")
    return f"HTTP {resp.status_code} code={code} {message}"
