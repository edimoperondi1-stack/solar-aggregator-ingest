"""ingest/notify.py -- fiacao env -> `WhatsAppNotifier`, e um CLI de teste.

Tudo que e ESPECIFICO DE AMBIENTE (nomes de secret, parse da lista de
destinatarios) mora aqui, nao em `notifiers/`, pelo mesmo motivo que
`_build_connector` mora em `ingest/run.py`: os modulos de `notifiers/` ficam
puros e testaveis sem env.

O canal e OPCIONAL por construcao: sem `WHATSAPP_TOKEN` /
`WHATSAPP_PHONE_NUMBER_ID` / `WHATSAPP_ALERT_TO` no ambiente,
`build_notifier_from_env()` devolve None e a ingestao roda exatamente como
antes. Isso mantem `--dry-run`, os testes e qualquer checkout novo do repo
funcionando sem nenhum secret novo -- notificacao nunca vira pre-requisito
de ingestao.

CLI de verificacao (nao toca no Postgres):

    python -m ingest.notify --test
    python -m ingest.notify --test --mode text   # so dentro da janela de 24h

SECURITY: nunca imprimir token nem telefone completo (logs de Actions deste
repo sao PUBLICOS) -- so `mask_phone()`.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from notifiers.base import AlarmEvent, mask_phone, normalize_phone
from notifiers.whatsapp.client import WhatsAppClient
from notifiers.whatsapp.notifier import (
    DEFAULT_LANGUAGE,
    DEFAULT_TEMPLATE_NAME,
    MODE_TEMPLATE,
    MODE_TEXT,
    VALID_MODES,
    WhatsAppNotifier,
)

ENV_TOKEN = "WHATSAPP_TOKEN"
ENV_PHONE_NUMBER_ID = "WHATSAPP_PHONE_NUMBER_ID"
ENV_ALERT_TO = "WHATSAPP_ALERT_TO"
ENV_TEMPLATE_NAME = "WHATSAPP_TEMPLATE_NAME"
ENV_TEMPLATE_LANG = "WHATSAPP_TEMPLATE_LANG"
ENV_MESSAGE_MODE = "WHATSAPP_MESSAGE_MODE"

_REQUIRED = (ENV_TOKEN, ENV_PHONE_NUMBER_ID, ENV_ALERT_TO)

# E.164: no maximo 15 digitos; o minimo real de um numero com DDI+DDD e bem
# maior que 8, mas 8 e um piso seguro que ainda pega os erros de digitacao
# obvios (um numero local sem DDI, por exemplo).
_MIN_DIGITS = 8
_MAX_DIGITS = 15


def parse_recipients(raw: str) -> list[str]:
    """CSV de telefones -> lista normalizada (so digitos), sem duplicatas e
    preservando a ordem.

    A Cloud API quer o numero em formato internacional SEM `+` e sem
    separador (ex.: `5565999998888`). Aceitamos o que o humano escreveu com
    `+`, espaco, parenteses ou hifen e limpamos aqui -- um secret com
    `+55 (65) 99999-8888` nao deve virar uma falha silenciosa de entrega.

    Entradas com contagem de digitos implausivel sao DESCARTADAS com aviso
    (mascarado), nunca enviadas: mandar para um numero errado e pior que nao
    mandar.
    """
    recipients: list[str] = []
    for chunk in raw.split(","):
        digits = normalize_phone(chunk)
        if not digits:
            continue
        if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
            print(
                f"  (destinatario WhatsApp ignorado -- {len(digits)} digitos, "
                f"fora de {_MIN_DIGITS}..{_MAX_DIGITS}: {mask_phone(digits)})"
            )
            continue
        if digits not in recipients:
            recipients.append(digits)
    return recipients


def build_notifier_from_env() -> WhatsAppNotifier | None:
    """Monta o notificador a partir do ambiente, ou devolve None se o canal
    nao estiver configurado (caso normal de um checkout sem secrets).

    Nunca levanta por configuracao ausente -- imprime UMA linha dizendo o que
    falta (so o NOME da variavel, nunca valor) e devolve None.
    """
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        print(
            "WhatsApp desativado (variaveis ausentes: " + ", ".join(missing) + ")"
        )
        return None

    recipients = parse_recipients(os.environ[ENV_ALERT_TO])
    if not recipients:
        print(f"WhatsApp desativado ({ENV_ALERT_TO} nao tem numero valido)")
        return None

    mode = os.environ.get(ENV_MESSAGE_MODE, MODE_TEMPLATE).strip().lower()
    if mode not in VALID_MODES:
        print(
            f"WhatsApp: {ENV_MESSAGE_MODE}={mode!r} invalido -- "
            f"usando {MODE_TEMPLATE!r}"
        )
        mode = MODE_TEMPLATE

    client = WhatsAppClient(
        token=os.environ[ENV_TOKEN],
        phone_number_id=os.environ[ENV_PHONE_NUMBER_ID],
    )
    notifier = WhatsAppNotifier(
        client,
        recipients,
        template_name=os.environ.get(ENV_TEMPLATE_NAME, DEFAULT_TEMPLATE_NAME),
        language=os.environ.get(ENV_TEMPLATE_LANG, DEFAULT_LANGUAGE),
        mode=mode,
    )
    print(
        f"WhatsApp ativo: {len(recipients)} destinatario(s) "
        f"[{', '.join(mask_phone(r) for r in recipients)}] modo={mode}"
    )
    return notifier


def _test_event() -> AlarmEvent:
    """Evento sintetico do `--test`: deixa claro na propria mensagem que e
    teste, para ninguem sair checando uma usina que esta bem."""
    return AlarmEvent(
        transition="opened",
        plant_name="TESTE -- ignore este alerta",
        external_plant_id="TESTE",
        manufacturer="goodwe",
        alarm_type="offline",
        severity="warning",
        at_utc=datetime.now(timezone.utc),
        plant_tz="America/Cuiaba",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.notify",
        description=(
            "Verifica a configuracao de WhatsApp enviando uma mensagem de "
            "teste. Nao acessa o Postgres."
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Envia uma mensagem de teste para os destinatarios configurados.",
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=list(VALID_MODES),
        help=(
            f"Sobrescreve {ENV_MESSAGE_MODE} nesta execucao. {MODE_TEXT!r} so "
            f"funciona nas 24h seguintes a uma mensagem SUA para o numero do bot."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)

    if args.mode:
        os.environ[ENV_MESSAGE_MODE] = args.mode

    notifier = build_notifier_from_env()
    if notifier is None:
        print("Nada a enviar -- configure as variaveis acima (ver README).")
        return 1

    if not args.test:
        print("Configuracao OK. Use --test para enviar uma mensagem de teste.")
        return 0

    sent = notifier.send(_test_event())
    print(f"Mensagens aceitas pela Meta: {sent}")
    # Zero aceitas = falha real de configuracao (token/template/numero);
    # exit != 0 para o teste manual ser honesto sobre isso.
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
