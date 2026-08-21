"""ingest/alarms.py -- deteccao + ciclo de vida idempotente dos dois alarmes
derivados de dados (METR-03, plano 04-03): `offline` e `zero_generation`.

Roda a cada ciclo de ingestao de 15 min, como passo pos-ingestao dentro de
`ingest/run.py` (D-16) -- so assim os limiares de 1-2h (D-10/D-11) sao
honrados; um job diario so veria a queda no dia seguinte. Ambos os alarmes
sao avaliados apenas dentro da janela solar da usina (pvlib, via
`ingest.solar_window`) para nunca disparar de noite.

`offline`: sem leitura nova por ~2h (8 ciclos) com sol de pe (severity
warning, D-10). `zero_generation`: power_w ~= 0 por >=4 ciclos consecutivos
(~1h) dentro da janela solar com margem (severity critical, D-11). Ambos
sao automaticos -- o job abre quando a condicao comeca e fecha
(`closed_at`) sozinho quando os dados/geracao voltam (D-12); nunca fechado
manualmente. O SQL de abertura/fechamento mira o indice parcial
`alarms_one_open_per_target` (uma linha aberta por alvo) verbatim (Pitfall 3
do 04-RESEARCH.md).

SECURITY: nunca logar/imprimir connection string ou qualquer segredo (ver
CLAUDE.md "Secrets Handling").
"""

from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Constantes documentadas -- unica fonte de verdade para os limiares (D-10/
# D-11, Open Question #3 do 04-RESEARCH.md). Nunca espalhar literais.
# ---------------------------------------------------------------------------
ZERO_GEN_WATTS = 50  # power_w <= isto conta como "quase zero" (D-11). Limiar
# absoluto fixo; as usinas piloto sao 7-13.5 kWp, entao um valor fixo nao
# gera falso positivo/negativo. Mantido como UMA constante para trocar por
# uma regra relativa a capacidade mais tarde ser trivial.
OFFLINE_THRESHOLD = timedelta(hours=2)  # ~8 ciclos de 15 min (D-10)
ZERO_GEN_MIN_CYCLES = 4  # >=4 ciclos quase-zero consecutivos (~1h) (D-11)
ZERO_GEN_MARGIN_HOURS = 1.0  # exclui a 1a/ultima hora de sol raso (D-11)


def is_offline(last_watermark: datetime | None, now_utc: datetime) -> bool:
    """D-10: nunca recebeu leitura (last_watermark None) -> True. Caso
    contrario, True se ja se passaram >= OFFLINE_THRESHOLD desde a ultima
    leitura."""
    if last_watermark is None:
        return True
    return (now_utc - last_watermark) >= OFFLINE_THRESHOLD


def within_solar_window(
    now_utc: datetime, sunrise_utc: datetime, sunset_utc: datetime
) -> bool:
    """D-10: so avaliar offline/zero_generation quando o sol esta de pe
    para esta usina (nunca alarmar de noite)."""
    return sunrise_utc <= now_utc <= sunset_utc


def is_zero_generation(power_values: list[float]) -> bool:
    """D-11: True sse existir uma sequencia de >= ZERO_GEN_MIN_CYCLES
    valores CONSECUTIVOS todos <= ZERO_GEN_WATTS.

    Este predicado opera sobre leituras ja filtradas pelo chamador para a
    janela solar com margem (via `solar_window_utc(...,
    margin_hours=ZERO_GEN_MARGIN_HOURS)`) -- ele mesmo nao sabe de horario.
    """
    run_length = 0
    for value in power_values:
        if value <= ZERO_GEN_WATTS:
            run_length += 1
            if run_length >= ZERO_GEN_MIN_CYCLES:
                return True
        else:
            run_length = 0
    return False


# ---------------------------------------------------------------------------
# SQL idempotente de abertura/fechamento, copiando o alvo do indice
# VERBATIM de db/migrations/004_alarms.sql (Pitfall 3 -- a lista de colunas
# E o `WHERE closed_at IS NULL` devem bater exatamente). `alarms` NAO e
# particionada, entao ON CONFLICT direto funciona (sem o workaround de
# CTE/xmax exigido por `readings`).
# ---------------------------------------------------------------------------
OPEN_ALARM_SQL = """
INSERT INTO alarms (tenant_id, plant_id, device_id, manufacturer, alarm_type, severity, opened_at)
VALUES (%(tenant_id)s, %(plant_id)s, %(device_id)s, %(manufacturer)s, %(alarm_type)s, %(severity)s, %(opened_at)s)
ON CONFLICT (tenant_id, plant_id, device_id, alarm_type) WHERE closed_at IS NULL
DO NOTHING;
"""

CLOSE_ALARM_SQL = """
UPDATE alarms SET closed_at = %(closed_at)s
WHERE tenant_id = %(tenant_id)s AND plant_id = %(plant_id)s
  AND device_id IS NOT DISTINCT FROM %(device_id)s
  AND alarm_type = %(alarm_type)s AND closed_at IS NULL;
"""


def open_alarm(
    cur,
    *,
    tenant_id: int,
    plant_id: int,
    device_id: int | None,
    manufacturer: str,
    alarm_type: str,
    severity: str,
    opened_at: datetime,
) -> None:
    """Abre um alarme de forma idempotente (D-12): se ja existir uma linha
    aberta para este alvo (tenant, plant, device, alarm_type), ON CONFLICT
    DO NOTHING evita duplicata -- honra alarms_one_open_per_target. `offline`
    e `zero_generation` sao sempre no nivel da usina -> device_id=None
    (o `NULLS NOT DISTINCT` do indice torna isso seguro contra conflito)."""
    cur.execute(
        OPEN_ALARM_SQL,
        dict(
            tenant_id=tenant_id,
            plant_id=plant_id,
            device_id=device_id,
            manufacturer=manufacturer,
            alarm_type=alarm_type,
            severity=severity,
            opened_at=opened_at,
        ),
    )


def close_alarm(
    cur,
    *,
    tenant_id: int,
    plant_id: int,
    device_id: int | None,
    alarm_type: str,
    closed_at: datetime,
) -> None:
    """Fecha (D-12) a linha aberta deste alvo, se houver -- idempotente:
    rodar de novo depois de ja fechado casa 0 linhas, sem erro. `IS NOT
    DISTINCT FROM` casa device_id=NULL corretamente (comparacao normal com
    NULL nunca seria verdadeira)."""
    cur.execute(
        CLOSE_ALARM_SQL,
        dict(
            tenant_id=tenant_id,
            plant_id=plant_id,
            device_id=device_id,
            alarm_type=alarm_type,
            closed_at=closed_at,
        ),
    )


# ---------------------------------------------------------------------------
# Leitura de apoio para is_zero_generation (D-11): power_w da usina dentro da
# janela solar com margem, ordenado por ts. Fica aqui (nao em ingest/run.py)
# para manter toda a logica/SQL de alarme num so lugar (run.py so orquestra).
# ---------------------------------------------------------------------------
POWER_READINGS_SQL = """
SELECT power_w FROM readings
WHERE plant_id = %(plant_id)s AND ts >= %(window_start_utc)s AND ts <= %(window_end_utc)s
  AND power_w IS NOT NULL
ORDER BY ts;
"""


def fetch_power_readings(
    cur, plant_id: int, window_start_utc: datetime, window_end_utc: datetime
) -> list[float]:
    """power_w da usina (todos os devices) dentro de [window_start_utc,
    window_end_utc], ordenado por ts -- entrada de `is_zero_generation`."""
    cur.execute(
        POWER_READINGS_SQL,
        dict(
            plant_id=plant_id,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
        ),
    )
    return [float(row[0]) for row in cur.fetchall()]
