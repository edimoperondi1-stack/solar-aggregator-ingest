"""notifiers/base.py -- o seam de NOTIFICACAO, espelhando `connectors/base.py`.

`AlarmEvent` e o DTO unico que descreve UMA TRANSICAO de alarme (abriu ou
fechou) -- nunca "o estado atual de um alarme". Essa distincao e o coracao do
anti-spam: `ingest/alarms.py` ja abre/fecha de forma idempotente
(`ON CONFLICT DO NOTHING` na abertura, `WHERE closed_at IS NULL` no
fechamento), entao `cur.rowcount > 0` marca exatamente a BORDA -- o ciclo em
que o alarme de fato mudou de estado. Um alarme que segue aberto por 6 horas
casa 0 linhas nos 23 ciclos seguintes e portanto gera UMA notificacao, nao 24.

Por isso nao existe (nem e preciso) uma coluna `notified_at` em `alarms`:
este repo NUNCA cria ou altera schema (ver README, "Migracoes de banco") --
o DDL vive no repo privado. A idempotencia da notificacao e herdada da
idempotencia que o banco ja garante.

`Notifier` e o Protocol que cada canal implementa (hoje WhatsApp; amanha
e-mail/Telegram/webhook entram sem tocar em `ingest/run.py`).

SECURITY: nenhuma implementacao pode logar token, connection string ou
telefone completo -- os logs do GitHub Actions deste repo sao PUBLICOS (o
repo e publico justamente para ter Actions gratis). Use `mask_phone()`.
"""

from datetime import datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, field_validator

# Rotulo humano por alarm_type. Os tipos sao os mesmos dois de
# `ingest/alarms.py` (METR-03); um tipo desconhecido NAO quebra a
# notificacao -- cai no fallback do .get() em `alarm_label()`, porque
# perder o alerta seria pior que um rotulo feio.
ALARM_LABELS = {
    "offline": "Usina offline (sem leitura nova)",
    "zero_generation": "Geracao zero durante o dia",
}

# Palavra de status por transicao -- entra como 1o parametro do template
# aprovado na Meta (ver README) e no texto livre.
TRANSITION_WORDS = {
    "opened": "ABERTO",
    "closed": "RESOLVIDO",
}


def alarm_label(alarm_type: str) -> str:
    """Rotulo legivel de um alarm_type; devolve o proprio tipo se
    desconhecido (nunca levanta -- ver nota no topo)."""
    return ALARM_LABELS.get(alarm_type, alarm_type)


def normalize_phone(phone: str) -> str:
    """Telefone -> so digitos, como a Cloud API exige (`5565999998888`).

    Fonte unica de verdade, usada TANTO por `ingest.notify.parse_recipients`
    (que ainda valida tamanho e deduplica) QUANTO pelo proprio
    `WhatsAppClient` na hora de enviar. A duplicacao e proposital: sem ela,
    quem construisse o notificador direto -- sem passar pelo parse de env --
    mandaria `+55 (65) 99999-8888` no campo `to` e a Meta recusaria.
    """
    return "".join(ch for ch in phone if ch.isdigit())


def mask_phone(phone: str) -> str:
    """Telefone mascarado para log/excecao: so os 4 ultimos digitos.

    Os logs de Actions deste repo sao publicos, e o mascaramento automatico
    do GitHub e por valor LITERAL do secret -- ele nao cobre um numero
    individual extraido de uma lista CSV. Entao mascaramos na origem.
    """
    digits = normalize_phone(phone)
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


class AlarmEvent(BaseModel):
    """Uma transicao de alarme pronta para virar mensagem.

    `at_utc` e tz-aware UTC (o mesmo instante unico do ciclo, D-16);
    `plant_tz` e o IANA da usina, usado so na hora de formatar -- a mensagem
    mostra a hora LOCAL do cliente, nunca UTC, porque quem le e o operador.
    """

    transition: Literal["opened", "closed"]
    plant_name: str
    external_plant_id: str
    manufacturer: str
    alarm_type: str
    severity: str
    at_utc: datetime
    plant_tz: str

    @field_validator("at_utc")
    @classmethod
    def at_utc_must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("at_utc must be tz-aware (UTC)")
        return v

    def local_time_str(self) -> str:
        """Instante do evento no fuso da usina, formato brasileiro."""
        return self.at_utc.astimezone(ZoneInfo(self.plant_tz)).strftime(
            "%d/%m/%Y %H:%M"
        )

    def status_word(self) -> str:
        return TRANSITION_WORDS[self.transition]

    def template_params(self) -> list[str]:
        """Os 5 parametros do corpo do template aprovado, NESTA ordem (o
        README documenta o texto do template que os consome -- mudar a ordem
        aqui exige reaprovar o template na Meta).

        Nenhum parametro pode ser string vazia ou conter quebra de linha: a
        Cloud API rejeita o envio inteiro (erro 132000/131008) nesse caso.
        Por isso o `or`-fallback e o replace de newline.
        """
        return [
            self.status_word(),
            _one_line(self.plant_name) or self.external_plant_id,
            _one_line(alarm_label(self.alarm_type)),
            _one_line(self.severity),
            self.local_time_str(),
        ]

    def text_body(self) -> str:
        """Mensagem em texto livre (modo `text`, so dentro da janela de 24h
        -- ver README). Multilinha e permitido aqui, ao contrario dos
        parametros de template."""
        icon = "\U0001f534" if self.transition == "opened" else "✅"
        return (
            f"{icon} ALARME {self.status_word()}\n"
            f"Usina: {self.plant_name or self.external_plant_id}\n"
            f"Problema: {alarm_label(self.alarm_type)}\n"
            f"Severidade: {self.severity}\n"
            f"Quando: {self.local_time_str()} ({self.plant_tz})"
        )


def _one_line(value: str) -> str:
    """Colapsa qualquer whitespace (inclusive \\n e \\t) em espacos simples --
    exigencia de parametro de template da Cloud API."""
    return " ".join(str(value).split())


class Notifier(Protocol):
    """Contrato de um canal de notificacao. `ingest/run.py` so conhece isto."""

    channel: str

    def send(self, event: AlarmEvent) -> int:
        """Entrega `event`; devolve quantas mensagens sairam de fato.
        Nunca deve levantar por falha de UM destinatario -- ver a doc de
        `WhatsAppNotifier.send`."""
        ...
