"""notifiers/whatsapp/notifier.py -- `AlarmEvent` -> mensagem WhatsApp.

Implementa o Protocol `notifiers.base.Notifier`. Fica entre `ingest/run.py`
(que so conhece o Protocol) e `WhatsAppClient` (que so sabe falar HTTP), do
mesmo jeito que `connectors/goodwe/connector.py` fica entre o CLI e o
`SemsClient`.

REGRA CENTRAL: `send()` NUNCA levanta. Uma falha de WhatsApp nao pode
derrubar -- nem sequer poluir -- a ingestao, que ja commitou leituras e
alarmes no Postgres antes de chegar aqui. Notificacao e o passo MENOS
critico do ciclo: perder um aviso e ruim, perder a leitura e pior. Falha de
um destinatario tambem nao impede os outros (um numero invalido na lista nao
silencia a equipe inteira).

SECURITY: nunca logar telefone completo -- so `mask_phone()`. Ver
`notifiers/base.py`.
"""

from notifiers.base import AlarmEvent, mask_phone

# Idioma do template aprovado na Meta. `pt_BR` e o codigo que a Meta usa para
# portugues do Brasil (nao `pt-BR` nem `pt`) -- errar aqui devolve
# 132001 "template does not exist in the translation".
DEFAULT_LANGUAGE = "pt_BR"
DEFAULT_TEMPLATE_NAME = "solar_alarm"

# Modos de envio. `template` e o unico que funciona para alarme automatico
# (mensagem iniciada pelo negocio); `text` existe para o teste manual dentro
# da janela de 24h -- ver notifiers/whatsapp/client.py.
MODE_TEMPLATE = "template"
MODE_TEXT = "text"
VALID_MODES = (MODE_TEMPLATE, MODE_TEXT)


class WhatsAppNotifier:
    """Envia uma transicao de alarme para cada destinatario configurado."""

    channel = "whatsapp"

    def __init__(
        self,
        client,
        recipients: list[str],
        template_name: str = DEFAULT_TEMPLATE_NAME,
        language: str = DEFAULT_LANGUAGE,
        mode: str = MODE_TEMPLATE,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(
                f"modo de mensagem invalido: {mode!r} (validos: {list(VALID_MODES)})"
            )
        self._client = client
        self._recipients = recipients
        self._template_name = template_name
        self._language = language
        self._mode = mode

    def send(self, event: AlarmEvent) -> int:
        """Entrega `event` a todos os destinatarios; devolve quantas
        mensagens a Meta ACEITOU (nao quantas foram lidas).

        Nunca levanta: cada destinatario e isolado no seu proprio try/except
        e uma falha vira uma linha de log com o telefone mascarado.
        """
        sent = 0
        for to in self._recipients:
            try:
                if self._mode == MODE_TEXT:
                    self._client.send_text(to, event.text_body())
                else:
                    self._client.send_template(
                        to,
                        self._template_name,
                        self._language,
                        event.template_params(),
                    )
                sent += 1
            except Exception as exc:
                # Sem `raise`: ver REGRA CENTRAL no topo do modulo.
                print(
                    f"  (FALHA ao notificar {mask_phone(to)} por WhatsApp: {exc})"
                )
        return sent

    def send_all(self, events: list[AlarmEvent]) -> int:
        """Conveniencia para o lote de um ciclo de ingestao. Mesma garantia:
        nunca levanta."""
        return sum(self.send(event) for event in events)
