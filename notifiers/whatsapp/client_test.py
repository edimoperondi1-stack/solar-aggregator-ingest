"""notifiers/whatsapp/client_test.py -- prova payload, retry e erros do
cliente da Cloud API, sem rede e sem credencial real (httpx.MockTransport).

Cobre:
  1. send_text monta o payload de texto que a Meta espera
  2. send_template monta components/body/parameters na ordem recebida
  3. a URL inclui versao fixada + phone_number_id
  4. o token vai no header Authorization: Bearer
  5. 200 sem `messages` e falha (nada foi enfileirado)
  6. 400 levanta WhatsAppError SEM repetir a chamada (nao adianta retentar)
  7. a mensagem de erro cita o motivo da Meta e o telefone MASCARADO
  8. 5xx e retentado e o sucesso seguinte e devolvido
  9. 429 e retentado (rate limit e transitorio)
 10. erro de transporte e retentado
 11. _is_retryable: 500/429 sim, 400/401 nao

Usage: `python -m pytest notifiers/whatsapp/client_test.py`.
"""

import os
import sys

import httpx

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ),
)

from notifiers.whatsapp.client import (
    WhatsAppClient,
    WhatsAppError,
    _is_retryable,
)

TOKEN = "tok-de-teste-nunca-real"
PHONE_ID = "123456789"
TO = "5565999998888"

OK_BODY = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": TO, "wa_id": TO}],
    "messages": [{"id": "wamid.TESTE"}],
}


def _client(handler) -> tuple[WhatsAppClient, list]:
    """Cliente com MockTransport; devolve tambem a lista de requests vistos."""
    seen = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request, len(seen))

    return (
        WhatsAppClient(
            TOKEN, PHONE_ID, transport=httpx.MockTransport(_wrapped)
        ),
        seen,
    )


def _always_ok(request, attempt):
    return httpx.Response(200, json=OK_BODY)


def test_send_text_builds_expected_payload():
    import json

    client, seen = _client(_always_ok)
    client.send_text(TO, "corpo do alerta")
    payload = json.loads(seen[0].content)
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == TO
    assert payload["type"] == "text"
    assert payload["text"]["body"] == "corpo do alerta"
    assert payload["text"]["preview_url"] is False


def test_send_template_builds_components_in_order():
    import json

    client, seen = _client(_always_ok)
    client.send_template(TO, "solar_alarm", "pt_BR", ["A", "B", "C"])
    payload = json.loads(seen[0].content)
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "solar_alarm"
    assert payload["template"]["language"] == {"code": "pt_BR"}
    body = payload["template"]["components"][0]
    assert body["type"] == "body"
    assert [p["text"] for p in body["parameters"]] == ["A", "B", "C"]
    assert all(p["type"] == "text" for p in body["parameters"])


def test_url_contains_pinned_version_and_phone_number_id():
    client, seen = _client(_always_ok)
    client.send_text(TO, "x")
    url = str(seen[0].url)
    assert url == f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages"


def test_token_goes_in_authorization_bearer_header():
    client, seen = _client(_always_ok)
    client.send_text(TO, "x")
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_send_normalizes_recipient_with_separators():
    """Regressao: um numero formatado (`+55 (65) ...`) chegando por um caminho
    que nao passou por parse_recipients ia cru no campo `to` e a Meta
    recusava. Agora o proprio cliente normaliza."""
    import json

    client, seen = _client(_always_ok)
    client.send_text("+55 (65) 99999-8888", "x")
    assert json.loads(seen[0].content)["to"] == "5565999998888"


def test_send_template_normalizes_recipient_with_separators():
    import json

    client, seen = _client(_always_ok)
    client.send_template("+55 65 99999-8888", "tpl", "pt_BR", ["a"])
    assert json.loads(seen[0].content)["to"] == "5565999998888"


def test_200_without_messages_is_a_failure():
    def handler(request, attempt):
        return httpx.Response(200, json={"messaging_product": "whatsapp"})

    client, _ = _client(handler)
    try:
        client.send_text(TO, "x")
        raise AssertionError("esperado WhatsAppError para 200 sem `messages`")
    except WhatsAppError as exc:
        assert "sem `messages`" in str(exc)


def test_400_raises_without_retry():
    def handler(request, attempt):
        return httpx.Response(
            400,
            json={"error": {"message": "Template name does not exist", "code": 132001}},
        )

    client, seen = _client(handler)
    try:
        client.send_text(TO, "x")
        raise AssertionError("esperado WhatsAppError para 400")
    except WhatsAppError:
        pass
    # O ponto do teste: UMA tentativa so. Retentar um 400 so atrasa o ciclo.
    assert len(seen) == 1


def test_error_message_quotes_reason_and_masks_phone():
    def handler(request, attempt):
        return httpx.Response(
            400, json={"error": {"message": "Invalid parameter", "code": 100}}
        )

    client, _ = _client(handler)
    try:
        client.send_text(TO, "x")
        raise AssertionError("esperado WhatsAppError")
    except WhatsAppError as exc:
        text = str(exc)
        assert "Invalid parameter" in text
        assert "code=100" in text
        # O telefone completo NUNCA pode aparecer (log publico de Actions).
        assert TO not in text
        assert "8888" in text


def test_5xx_is_retried_then_succeeds():
    def handler(request, attempt):
        if attempt == 1:
            return httpx.Response(500, json={"error": {"message": "oops"}})
        return httpx.Response(200, json=OK_BODY)

    client, seen = _client(handler)
    data = client.send_text(TO, "x")
    assert data["messages"][0]["id"] == "wamid.TESTE"
    assert len(seen) == 2


def test_429_is_retried_then_succeeds():
    def handler(request, attempt):
        if attempt == 1:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, json=OK_BODY)

    client, seen = _client(handler)
    client.send_text(TO, "x")
    assert len(seen) == 2


def test_transport_error_is_retried_then_succeeds():
    def handler(request, attempt):
        if attempt == 1:
            raise httpx.ConnectError("conexao caiu", request=request)
        return httpx.Response(200, json=OK_BODY)

    client, seen = _client(handler)
    client.send_text(TO, "x")
    assert len(seen) == 2


def test_is_retryable_predicate():
    req = httpx.Request("POST", "https://example.invalid")

    def status_exc(code):
        return httpx.HTTPStatusError(
            "x", request=req, response=httpx.Response(code, request=req)
        )

    assert _is_retryable(httpx.ConnectError("x", request=req)) is True
    assert _is_retryable(status_exc(500)) is True
    assert _is_retryable(status_exc(503)) is True
    assert _is_retryable(status_exc(429)) is True
    assert _is_retryable(status_exc(400)) is False
    assert _is_retryable(status_exc(401)) is False
    assert _is_retryable(ValueError("nao e erro http")) is False


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("todos os testes passaram")
