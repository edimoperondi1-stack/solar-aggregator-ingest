"""ingest/notify_test.py -- prova o parse de destinatarios e a fiacao por env.

O ponto mais importante aqui e o DEGRADE GRACIOSO: sem os secrets WHATSAPP_*,
`build_notifier_from_env()` devolve None em vez de levantar -- e o que mantem
a ingestao (e todo checkout novo do repo) funcionando sem nenhum secret novo.

Cobre:
  1. parse_recipients limpa +, espaco, parenteses e hifen
  2. parse_recipients aceita CSV com varios numeros
  3. parse_recipients remove duplicata preservando a ordem
  4. parse_recipients descarta numero curto demais (erro de digitacao)
  5. parse_recipients descarta numero longo demais (> 15, fora do E.164)
  6. parse_recipients de string vazia devolve []
  7. build_notifier_from_env sem nenhuma var devolve None
  8. build_notifier_from_env com token/phone mas sem destinatario devolve None
  9. build_notifier_from_env completo devolve um notificador em modo template
 10. WHATSAPP_MESSAGE_MODE=text e respeitado
 11. modo invalido cai para template em vez de quebrar
 12. nem o token nem o telefone completo aparecem no stdout da construcao

Usage: `python -m pytest ingest/notify_test.py`.
"""

import io
import os
import sys
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.notify import build_notifier_from_env, parse_recipients

TOKEN = "tok-secreto-de-teste"
PHONE_ID = "123456789"
TO = "5565999998888"

FULL_ENV = {
    "WHATSAPP_TOKEN": TOKEN,
    "WHATSAPP_PHONE_NUMBER_ID": PHONE_ID,
    "WHATSAPP_ALERT_TO": TO,
}


def test_parse_recipients_strips_separators():
    assert parse_recipients("+55 (65) 99999-8888") == ["5565999998888"]


def test_parse_recipients_handles_csv():
    assert parse_recipients("5565999998888,5511988887777") == [
        "5565999998888",
        "5511988887777",
    ]


def test_parse_recipients_dedupes_preserving_order():
    raw = "5565999998888, 5511988887777, +55 65 99999-8888"
    assert parse_recipients(raw) == ["5565999998888", "5511988887777"]


def test_parse_recipients_drops_too_short():
    assert parse_recipients("998888") == []


def test_parse_recipients_drops_too_long():
    assert parse_recipients("1" * 16) == []


def test_parse_recipients_empty_string():
    assert parse_recipients("") == []


def test_build_notifier_returns_none_without_env():
    with mock.patch.dict(os.environ, {}, clear=True):
        with redirect_stdout(io.StringIO()):
            assert build_notifier_from_env() is None


def test_build_notifier_returns_none_without_recipients():
    env = dict(FULL_ENV, WHATSAPP_ALERT_TO="   ")
    with mock.patch.dict(os.environ, env, clear=True):
        with redirect_stdout(io.StringIO()):
            assert build_notifier_from_env() is None


def test_build_notifier_full_env_returns_template_notifier():
    with mock.patch.dict(os.environ, FULL_ENV, clear=True):
        with redirect_stdout(io.StringIO()):
            notifier = build_notifier_from_env()
    assert notifier is not None
    assert notifier.channel == "whatsapp"
    assert notifier._mode == "template"
    assert notifier._recipients == [TO]


def test_build_notifier_honours_text_mode():
    env = dict(FULL_ENV, WHATSAPP_MESSAGE_MODE="text")
    with mock.patch.dict(os.environ, env, clear=True):
        with redirect_stdout(io.StringIO()):
            notifier = build_notifier_from_env()
    assert notifier._mode == "text"


def test_build_notifier_invalid_mode_falls_back_to_template():
    env = dict(FULL_ENV, WHATSAPP_MESSAGE_MODE="sms")
    with mock.patch.dict(os.environ, env, clear=True):
        with redirect_stdout(io.StringIO()):
            notifier = build_notifier_from_env()
    # Nao levanta: um valor errado de env nao pode silenciar os alertas.
    assert notifier is not None
    assert notifier._mode == "template"


def test_build_notifier_never_prints_token_or_full_phone():
    buf = io.StringIO()
    with mock.patch.dict(os.environ, FULL_ENV, clear=True):
        with redirect_stdout(buf):
            build_notifier_from_env()
    out = buf.getvalue()
    assert TOKEN not in out
    assert TO not in out
    assert "8888" in out  # so o sufixo mascarado


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("todos os testes passaram")
