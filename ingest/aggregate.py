"""ingest/aggregate.py -- consolidacao diaria de energia/disponibilidade/
alarme_count (METR-01/METR-02, plano 04-02).

Le a serie crua de 15 min em `readings` e grava um agregado permanente por
usina/dia em `readings_daily`: energia diaria (D-06/D-07: soma de
MAX(energy_day_kwh) por device, com fallback trapezoidal quando o contador do
vendor estiver ausente), disponibilidade de dados (D-04/D-05: fracao dos
ticks de 15 min da janela solar -- pvlib -- com leitura presente) e
alarm_count (D-17: alarmes abertos naquele dia local). `readings_daily.pr`
NUNCA e escrito por este job -- fica NULL intencionalmente (D-02): PR real via
irradiancia fica para uma fase futura, e uma aproximacao rotulada como PR
seria desonesta.

SECURITY: nunca logar/imprimir a connection string ou qualquer segredo (ver
CLAUDE.md "Secrets Handling"). Apenas nomes de usina, external_plant_id,
contagens e status sao impressos.
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from dotenv import load_dotenv

from connectors.common.db import connect_as_ingestion_writer, get_plants_by_manufacturer
from ingest.solar_window import get_plant_coords, solar_window_utc

# ---------------------------------------------------------------------------
# Compute layer (pure functions -- testadas sem DB em aggregate_test.py)
# ---------------------------------------------------------------------------


def energy_kwh_from_power_trapezoid(
    timestamps: list[datetime], power_w: list[float]
) -> float:
    """D-07: fallback de energia quando `energy_day_kwh` do vendor esta
    ausente para o device/dia -- integra `power_w` no tempo (trapezoidal).

    MUST usar `np.trapezoid` -- o nome antigo (`trap` + `z`, removido no
    numpy 2.4+; numpy resolvido deste projeto e 2.5.1) levantaria
    AttributeError.
    """
    if len(timestamps) < 2:
        return 0.0
    t0 = timestamps[0]
    hours = [(t - t0).total_seconds() / 3600 for t in timestamps]
    watt_hours = np.trapezoid(power_w, x=hours)
    return float(watt_hours) / 1000.0  # Wh -> kWh


def plant_day_energy_kwh(
    device_max_energy_kwh: dict, device_fallback_kwh: dict | None = None
) -> float:
    """D-06: soma `MAX(energy_day_kwh)` por device de uma usina/dia. Um
    device cujo MAX seja None (contador do vendor ausente para o dia) usa
    seu valor de fallback trapezoidal (D-07) em vez de ser descartado --
    `device_fallback_kwh` deve trazer uma entrada para cada device None
    (default 0.0 se ausente)."""
    fallback = device_fallback_kwh or {}
    total = 0.0
    for device_id, max_energy in device_max_energy_kwh.items():
        if max_energy is not None:
            total += float(max_energy)
        else:
            total += float(fallback.get(device_id, 0.0))
    return total


def recompute_window_days(local_today: date, n: int = 4) -> list[date]:
    """D-15: janela de recomputo self-healing -- hoje (parcial) + os ultimos
    `n - 1` dias, do mais recente para o mais antigo. Idempotente: rodar de
    novo so reescreve os mesmos dias via upsert."""
    return [local_today - timedelta(days=i) for i in range(n)]


def local_day_utc_bounds(local_day: date, tz: str) -> tuple[datetime, datetime]:
    """Converte um dia calendario LOCAL (fuso da usina) para os limites UTC
    [inicio, fim) desse dia -- via ZoneInfo em Python, nunca timedelta fixo
    (nao confiar no Postgres para conhecer transicoes de fuso IANA)."""
    zone = ZoneInfo(tz)
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(local_day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    return start, end


# ---------------------------------------------------------------------------
# Energia: MAX(energy_day_kwh) por device/dia local (Pattern 3), bucketado
# via AT TIME ZONE (coluna nao-literal, joined de plants.timezone).
# ---------------------------------------------------------------------------
ENERGY_MAX_BY_DEVICE_DAY_SQL = """
SELECT r.device_id, (r.ts AT TIME ZONE p.timezone)::date AS local_day,
       MAX(r.energy_day_kwh) AS device_max_energy_kwh
FROM readings r JOIN plants p ON p.id = r.plant_id
WHERE r.plant_id = %(plant_id)s AND r.ts >= %(win_start_utc)s AND r.ts < %(win_end_utc)s
GROUP BY r.device_id, local_day;
"""

DEVICE_RAW_POWER_SQL = """
SELECT ts, power_w FROM readings
WHERE plant_id = %(plant_id)s AND device_id = %(device_id)s
  AND ts >= %(day_start_utc)s AND ts < %(day_end_utc)s
ORDER BY ts;
"""

# Alguma leitura existe naquele dia local? (D-08: so grava readings_daily
# quando a usina tem >=1 leitura no dia -- nao fabricar dias vazios).
PLANT_HAS_ANY_READING_SQL = """
SELECT 1 FROM readings
WHERE plant_id = %(plant_id)s AND ts >= %(day_start_utc)s AND ts < %(day_end_utc)s
LIMIT 1;
"""

# ---------------------------------------------------------------------------
# Disponibilidade: fracao dos ticks de 15 min da janela solar (pvlib) com
# leitura presente (power_w NOT NULL, D-05). date_bin (Postgres 14+, ok no
# Supabase PG15+) tolera pontos do SEMS que nao caem exatamente em
# :00/:15/:30/:45 (Pitfall 4 / Open Question 1).
# ---------------------------------------------------------------------------
AVAILABILITY_SQL = """
WITH expected AS (
  SELECT generate_series(%(sunrise_utc)s::timestamptz, %(sunset_utc)s::timestamptz, interval '15 min') AS tick
),
present AS (
  SELECT DISTINCT date_bin('15 min', ts, %(sunrise_utc)s::timestamptz) AS bucket
  FROM readings
  WHERE plant_id = %(plant_id)s AND ts >= %(sunrise_utc)s AND ts <= %(sunset_utc)s
    AND power_w IS NOT NULL
)
SELECT count(present.bucket)::numeric / NULLIF(count(expected.tick), 0) AS availability_pct
FROM expected
LEFT JOIN present ON present.bucket = date_bin('15 min', expected.tick, %(sunrise_utc)s::timestamptz);
"""


def fetch_device_max_energy_by_day(
    cur, plant_id: int, win_start_utc: datetime, win_end_utc: datetime
) -> dict:
    """Roda ENERGY_MAX_BY_DEVICE_DAY_SQL para a janela inteira de recomputo
    e agrupa por dia local: {local_day: {device_id: max_energy_kwh|None}}."""
    cur.execute(
        ENERGY_MAX_BY_DEVICE_DAY_SQL,
        dict(plant_id=plant_id, win_start_utc=win_start_utc, win_end_utc=win_end_utc),
    )
    result: dict = {}
    for device_id, local_day, device_max_energy_kwh in cur.fetchall():
        result.setdefault(local_day, {})[device_id] = (
            float(device_max_energy_kwh) if device_max_energy_kwh is not None else None
        )
    return result


def fetch_device_raw_power(
    cur, plant_id: int, device_id: int, day_start_utc: datetime, day_end_utc: datetime
) -> tuple:
    """Le (ts, power_w) brutos de um device/dia para o fallback trapezoidal
    (D-07)."""
    cur.execute(
        DEVICE_RAW_POWER_SQL,
        dict(
            plant_id=plant_id,
            device_id=device_id,
            day_start_utc=day_start_utc,
            day_end_utc=day_end_utc,
        ),
    )
    rows = cur.fetchall()
    timestamps = [row[0] for row in rows]
    power_w = [float(row[1]) if row[1] is not None else 0.0 for row in rows]
    return timestamps, power_w


def compute_plant_day_energy_kwh(
    cur,
    plant_id: int,
    day_start_utc: datetime,
    day_end_utc: datetime,
    device_max_energy: dict,
) -> float:
    """Combina o MAX por device com o fallback trapezoidal (D-07) para
    devices com MAX ausente, e soma (D-06) via plant_day_energy_kwh (pura)."""
    fallback: dict = {}
    for device_id, max_energy in device_max_energy.items():
        if max_energy is None:
            timestamps, power_w = fetch_device_raw_power(
                cur, plant_id, device_id, day_start_utc, day_end_utc
            )
            fallback[device_id] = energy_kwh_from_power_trapezoid(timestamps, power_w)
    return plant_day_energy_kwh(device_max_energy, fallback)


def plant_has_any_reading(
    cur, plant_id: int, day_start_utc: datetime, day_end_utc: datetime
) -> bool:
    """D-08: so grava readings_daily quando a usina tem >=1 leitura naquele
    dia local -- nao fabricar dias vazios."""
    cur.execute(
        PLANT_HAS_ANY_READING_SQL,
        dict(plant_id=plant_id, day_start_utc=day_start_utc, day_end_utc=day_end_utc),
    )
    return cur.fetchone() is not None


def compute_availability_pct(
    cur, plant_id: int, sunrise_utc: datetime, sunset_utc: datetime
) -> float | None:
    """D-04/D-05: fracao dos ticks de 15 min entre sunrise/sunset (pvlib,
    margin_hours=0) com leitura presente (power_w NOT NULL). Quem chama deve
    pular usinas sem coordenadas (get_plant_coords() -> None) ANTES de
    chegar aqui -- nunca usar (0,0) como default geografico."""
    cur.execute(
        AVAILABILITY_SQL,
        dict(plant_id=plant_id, sunrise_utc=sunrise_utc, sunset_utc=sunset_utc),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


# alarm_count (D-17, Open Question #2 default): alarmes ABERTOS naquele dia
# local (opened_at dentro dos limites UTC do dia).
ALARM_COUNT_SQL = """
SELECT count(*) FROM alarms
WHERE tenant_id = %(tenant_id)s AND plant_id = %(plant_id)s
  AND opened_at >= %(day_start_utc)s AND opened_at < %(day_end_utc)s;
"""

# readings_daily: upsert idempotente na PK (tenant_id, plant_id, day).
# `pr` NUNCA aparece na lista de colunas (D-02) -- fica NULL pelo default
# da coluna, honrando o contrato de "nunca rotular aproximacao como PR".
# readings_daily NAO e particionada -- ON CONFLICT direto, sem o workaround
# de CTE/xmax exigido pela `readings` particionada (connectors/common/db.py).
UPSERT_READINGS_DAILY_SQL = """
INSERT INTO readings_daily (tenant_id, plant_id, manufacturer, day,
                            energy_kwh, availability_pct, alarm_count)
VALUES (%(tenant_id)s, %(plant_id)s, %(manufacturer)s, %(day)s,
        %(energy_kwh)s, %(availability_pct)s, %(alarm_count)s)
ON CONFLICT (tenant_id, plant_id, day) DO UPDATE SET
  energy_kwh = EXCLUDED.energy_kwh,
  availability_pct = EXCLUDED.availability_pct,
  alarm_count = EXCLUDED.alarm_count;
"""


def compute_alarm_count(
    cur, tenant_id: int, plant_id: int, day_start_utc: datetime, day_end_utc: datetime
) -> int:
    """D-17: numero de alarmes ABERTOS (opened_at) naquele dia local."""
    cur.execute(
        ALARM_COUNT_SQL,
        dict(
            tenant_id=tenant_id,
            plant_id=plant_id,
            day_start_utc=day_start_utc,
            day_end_utc=day_end_utc,
        ),
    )
    (count,) = cur.fetchone()
    return int(count)


def upsert_readings_daily(
    cur,
    tenant_id: int,
    plant_id: int,
    manufacturer: str,
    day: date,
    energy_kwh: float,
    availability_pct: float | None,
    alarm_count: int,
) -> None:
    """Upsert idempotente em readings_daily. `pr` nunca e referenciado aqui
    (D-02) -- fica NULL."""
    cur.execute(
        UPSERT_READINGS_DAILY_SQL,
        dict(
            tenant_id=tenant_id,
            plant_id=plant_id,
            manufacturer=manufacturer,
            day=day,
            energy_kwh=energy_kwh,
            availability_pct=availability_pct,
            alarm_count=alarm_count,
        ),
    )


# ---------------------------------------------------------------------------
# CLI / orquestracao
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingest.aggregate",
        description=(
            "Consolidacao diaria (METR-01/METR-02): energia, disponibilidade "
            "e alarm_count por usina/dia em readings_daily. pr fica NULL "
            "(D-02, deferred a fase futura)."
        ),
    )
    parser.add_argument(
        "--manufacturer",
        default="goodwe",
        help="Fabricante a processar (default: goodwe)",
    )
    parser.add_argument(
        "--recompute-days",
        type=int,
        default=4,
        metavar="N",
        help=(
            "Janela de recomputo self-healing: hoje (parcial) + os ultimos "
            "N-1 dias (default: 4 = hoje + ultimos 3, D-15)."
        ),
    )
    parser.add_argument(
        "--plant",
        default=None,
        metavar="EXTERNAL_PLANT_ID",
        help="Roda so esta usina (external_plant_id).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Computa e imprime, SEM gravar no banco (rollback de todas as escritas).",
    )
    return parser.parse_args(argv)


def _local_today(plant_tz: str) -> date:
    """Dia calendario CORRENTE no fuso IANA da usina (nunca UTC nem offset
    hardcoded) -- mesmo padrao de ingest/run.py."""
    return datetime.now(ZoneInfo(plant_tz)).date()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)

    conn = connect_as_ingestion_writer(os.environ["SUPABASE_INGESTION_URL"])
    conn.autocommit = False
    cur = conn.cursor()

    try:
        plants = get_plants_by_manufacturer(
            cur, args.manufacturer, only_external_id=args.plant
        )
        print(f"Usinas ({args.manufacturer}): {len(plants)}")

        total_days_processed = 0
        total_rows_upserted = 0
        failures = 0
        # (plant_name, day, energy_kwh, availability_pct, alarm_count) para o
        # RESUMO humano no final.
        per_plant_day_results: list[tuple] = []

        for plant in plants:
            tenant_id = plant["tenant_id"]
            plant_id = plant["id"]
            external_plant_id = plant["external_plant_id"]
            plant_tz = plant["timezone"]
            manufacturer = args.manufacturer
            name = plant["name"]

            print(
                f"  - {name} (external_plant_id={external_plant_id}, tz={plant_tz})"
            )

            try:
                local_today = _local_today(plant_tz)
                days = recompute_window_days(local_today, args.recompute_days)

                # Uma unica query cobre toda a janela de recomputo (Pattern 3).
                win_start_utc, _ = local_day_utc_bounds(days[-1], plant_tz)
                _, win_end_utc = local_day_utc_bounds(days[0], plant_tz)
                energy_by_day = fetch_device_max_energy_by_day(
                    cur, plant_id, win_start_utc, win_end_utc
                )

                coords = get_plant_coords(external_plant_id)
                if coords is None:
                    print(
                        f"    SEM coordenadas em config/plant_coordinates.toml -- "
                        f"disponibilidade ficara NULL para {name}"
                    )

                plant_rows = 0
                for day in days:
                    day_start_utc, day_end_utc = local_day_utc_bounds(day, plant_tz)

                    if not plant_has_any_reading(cur, plant_id, day_start_utc, day_end_utc):
                        continue  # D-08: nao fabricar dia vazio

                    device_max_energy = energy_by_day.get(day, {})
                    energy_kwh = compute_plant_day_energy_kwh(
                        cur, plant_id, day_start_utc, day_end_utc, device_max_energy
                    )

                    availability_pct = None
                    if coords is not None:
                        lat, lon = coords
                        sunrise_utc, sunset_utc = solar_window_utc(
                            lat, lon, plant_tz, day, margin_hours=0.0
                        )
                        availability_pct = compute_availability_pct(
                            cur, plant_id, sunrise_utc, sunset_utc
                        )

                    alarm_count = compute_alarm_count(
                        cur, tenant_id, plant_id, day_start_utc, day_end_utc
                    )

                    if not args.dry_run:
                        upsert_readings_daily(
                            cur,
                            tenant_id,
                            plant_id,
                            manufacturer,
                            day,
                            energy_kwh,
                            availability_pct,
                            alarm_count,
                        )
                    plant_rows += 1
                    total_days_processed += 1
                    per_plant_day_results.append(
                        (name, day, energy_kwh, availability_pct, alarm_count)
                    )

                if args.dry_run:
                    conn.rollback()
                    print(f"    {plant_rows} dia(s) computados (dry-run, nada gravado)")
                else:
                    conn.commit()
                    total_rows_upserted += plant_rows
                    print(f"    {plant_rows} dia(s) gravados em readings_daily")

            except Exception as exc:  # falha isolada por usina, nao aborta as demais
                failures += 1
                conn.rollback()
                print(f"  FALHA na usina {name}: {exc}")
                continue

        print()
        print("=== RESUMO ===")
        print(f"Usinas processadas: {len(plants)} (falhas: {failures})")
        print(f"Janela de recomputo: {args.recompute_days} dia(s) (D-15)")
        print(f"Dias processados: {total_days_processed}")
        if args.dry_run:
            print("(dry-run, nada gravado)")
        else:
            print(f"Linhas gravadas em readings_daily: {total_rows_upserted}")
        for plant_name, day, energy_kwh, availability_pct, alarm_count in per_plant_day_results:
            avail_str = (
                f"{availability_pct:.2%}" if availability_pct is not None else "NULL"
            )
            print(
                f"  {plant_name} {day.isoformat()}: energy_kwh={energy_kwh:.3f} "
                f"availability_pct={avail_str} alarm_count={alarm_count}"
            )

        # Exit 1 SOMENTE em outage total (houve usinas e TODAS falharam) --
        # mesmo contrato de exit code de ingest/run.py.
        if plants and failures == len(plants):
            return 1
        return 0
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
