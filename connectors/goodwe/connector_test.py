"""connectors/goodwe/connector_test.py -- proves GoodWeConnector.normalize()
(plan 02-04, Task 1).

Covers the 4 <behavior> cases from 02-04-PLAN.md:
  1. A 2-point intraday payload with naive local timestamps
     ("America/Cuiaba") normalizes to tz-aware UTC timestamps exactly +4h
     from the naive local value -- NOT +3h (Pitfall E: the pilot plant is
     America/Cuiaba/UTC-4, not America/Sao_Paulo/UTC-3).
  2. A point missing power/energy fields normalizes to None (never 0).
  3. An empty raw payload (the pilot plant's normal offline state) normalizes
     to [] without raising.
  4. A raw vendor status code (e.g. -1) is preserved as text in
     `status_raw`, unmapped.

Uses inline raw-payload fixtures (the per-point shape hypothesis from
02-SPIKE-FINDINGS.md/pygoodwe -- MEDIUM confidence, since the pilot plant was
offline during the live spike; see connector.py's module docstring).

Usage: `python -m pytest connectors/goodwe/connector_test.py` or
`python connectors/goodwe/connector_test.py` (direct-script fallback, no
pytest required).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Allows running this file directly (`python connectors/goodwe/connector_test.py`)
# as well as via `python -m pytest` -- makes the `connectors` package
# importable when sys.path[0] would otherwise be connectors/goodwe/, not the
# repo root.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from connectors.goodwe.connector import GoodWeConnector

PLANT_TZ = "America/Cuiaba"  # pilot plant -- UTC-4, no DST (Pitfall E)


def _connector() -> GoodWeConnector:
    # normalize() never touches self._client -- a None client is fine here.
    return GoodWeConnector(client=None)


def test_normalize_localizes_naive_local_to_utc_plus_4h_for_cuiaba():
    raw = [
        {"date": "07/19/2026 08:00:00", "pac": 1000},
        {"date": "07/19/2026 08:15:00", "pac": 1200},
    ]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 2
    assert readings[0].ts.tzinfo is not None, "ts must be tz-aware"
    assert readings[0].ts == datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    assert readings[1].ts == datetime(2026, 7, 19, 12, 15, tzinfo=timezone.utc)

    # Explicit proof of Pitfall E: naive local + 4h == UTC, NOT +3h.
    naive_local = datetime(2026, 7, 19, 8, 0)
    assert readings[0].ts.replace(tzinfo=None) - naive_local == timedelta(hours=4)
    assert readings[0].ts.replace(tzinfo=None) - naive_local != timedelta(hours=3)

    assert readings[0].power_w == 1000.0
    assert readings[1].power_w == 1200.0


def test_normalize_missing_power_and_energy_fields_become_none():
    raw = [{"date": "07/19/2026 08:00:00"}]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 1
    assert readings[0].power_w is None
    assert readings[0].energy_day_kwh is None
    assert readings[0].energy_total_kwh is None


def test_normalize_empty_payload_returns_empty_list_without_raising():
    readings = _connector().normalize(
        [], tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )
    assert readings == []


def test_normalize_preserves_raw_status_as_text_unmapped():
    raw = [{"date": "07/19/2026 08:00:00", "pac": 500, "status": -1}]
    readings = _connector().normalize(
        raw, tenant_id=1, plant_id=1, device_id=1, plant_tz=PLANT_TZ
    )

    assert len(readings) == 1
    assert readings[0].status_raw == "-1"
    assert isinstance(readings[0].status_raw, str)


def _run_all():
    tests = [
        test_normalize_localizes_naive_local_to_utc_plus_4h_for_cuiaba,
        test_normalize_missing_power_and_energy_fields_become_none,
        test_normalize_empty_payload_returns_empty_list_without_raising,
        test_normalize_preserves_raw_status_as_text_unmapped,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
