"""notifiers/whatsapp/notifier_test.py -- prova o fan-out e, sobretudo, a
GARANTIA DE NAO-PROPAGACAO: notificar nunca pode derrubar a ingestao.

Cobre:
  1. send() envia para TODOS os destinatarios e devolve a contagem
  2. modo template usa send_template com os params do evento
  3. modo text usa send_text com o corpo multilinha
  4. falha de UM destinatario nao impede os outros (e nao levanta)
  5. falha de TODOS devolve 0 e nao levanta
  6. send_all soma os eventos do ciclo
  7. send_all com lista vazia nao chama o cliente
  8. modo invalido no construtor levanta (erro de programacao, nao de runtime)
  9. o telefone completo nunca aparece no log de falha

Usage: `python -m pytest notifiers/whatsapp/notifier_test.py`.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ),
)

from notifiers.base import AlarmEvent
from notifiers.whatsapp.notifier import WhatsAppNotifier

TO_A = "5565999998888"
TO_B = "5511988887777"

EVENT = AlarmEvent(
    transition="opened",
    plant_name="Usina Piloto",
    external_plant_id="147493",
    manufacturer="goodwe",
    alarm_type="offline",
    severity="warning",
    at_utc=datetime(2026, 7, 21, 15, 5, 0, tzinfo=timezone.utc),
    plant_tz="America/Cuiaba",
)


class FakeClient:
    """Cliente de mentira que registra chamadas e pode falhar por numero."""

    def __init__(self, fail_for=()):
        self.texts = []
        self.templates = []
        self._fail_for = set(fail_for)

    def _maybe_fail(self, to):
        if to in self._fail_for:
            raise RuntimeError("Meta recusou")

    def send_text(self, to, body):
        self._maybe_fail(to)
        self.texts.append((to, body))
        return {"messages": [{"id": "x"}]}

    def send_template(self, to, template_name, language, body_params):
        self._maybe_fail(to)
        self.templates.append((to, template_name, language, body_params))
        return {"messages": [{"id": "x"}]}


def test_send_reaches_every_recipient():
    client = FakeClient()
    n = WhatsAppNotifier(client, [TO_A, TO_B]).send(EVENT)
    assert n == 2
    assert [t[0] for t in client.templates] == [TO_A, TO_B]


def test_template_mode_passes_event_params():
    client = FakeClient()
    WhatsAppNotifier(
        client, [TO_A], template_name="meu_tpl", language="pt_BR"
    ).send(EVENT)
    to, name, lang, params = client.templates[0]
    assert (name, lang) == ("meu_tpl", "pt_BR")
    assert params == EVENT.template_params()
    assert params[0] == "ABERTO"


def test_text_mode_sends_multiline_body():
    client = FakeClient()
    WhatsAppNotifier(client, [TO_A], mode="text").send(EVENT)
    to, body = client.texts[0]
    assert to == TO_A
    assert body == EVENT.text_body()
    assert not client.templates


def test_one_failing_recipient_does_not_block_the_others():
    client = FakeClient(fail_for=[TO_A])
    n = WhatsAppNotifier(client, [TO_A, TO_B]).send(EVENT)
    # TO_A falhou, TO_B passou -- e nada levantou.
    assert n == 1
    assert [t[0] for t in client.templates] == [TO_B]


def test_all_failing_returns_zero_and_does_not_raise():
    client = FakeClient(fail_for=[TO_A, TO_B])
    assert WhatsAppNotifier(client, [TO_A, TO_B]).send(EVENT) == 0


def test_send_all_sums_events():
    client = FakeClient()
    closed = EVENT.model_copy(update={"transition": "closed"})
    n = WhatsAppNotifier(client, [TO_A, TO_B]).send_all([EVENT, closed])
    assert n == 4
    assert len(client.templates) == 4


def test_send_all_empty_list_never_touches_client():
    client = FakeClient()
    assert WhatsAppNotifier(client, [TO_A]).send_all([]) == 0
    assert not client.templates and not client.texts


def test_invalid_mode_raises_at_construction():
    try:
        WhatsAppNotifier(FakeClient(), [TO_A], mode="sms")
        raise AssertionError("esperado ValueError para modo invalido")
    except ValueError as exc:
        assert "sms" in str(exc)


def test_failure_log_masks_phone(capsys=None):
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        WhatsAppNotifier(FakeClient(fail_for=[TO_A]), [TO_A]).send(EVENT)
    out = buf.getvalue()
    assert TO_A not in out
    assert "8888" in out


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("todos os testes passaram")
