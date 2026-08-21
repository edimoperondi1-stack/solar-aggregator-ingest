"""connectors/base_test.py -- prova o seam CONN-04 (connectors/base.py).

Cobre os 3 casos do plano 02-02 Task 1:
  1. NormalizedReading aceita dict valido com ts tz-aware; campos opcionais
     ausentes viram None (nao 0).
  2. ts NAIVE (sem tzinfo) levanta ValidationError.
  3. manufacturer default == "goodwe"; device_id e obrigatorio (sem default).

Uso: `python -m pytest connectors/base_test.py` ou `python connectors/base_test.py`.
"""

import os
import sys
from datetime import datetime, timezone

# Permite `python connectors/base_test.py` direto (sem -m) alem de
# `python -m pytest` / `python -m connectors.base_test` -- garante que o
# pacote `connectors` seja importavel mesmo quando este arquivo roda como
# script solto (sys.path[0] vira connectors/, nao a raiz do repo).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import ValidationError

from connectors.base import NormalizedReading


def test_valid_reading_tz_aware_optional_fields_are_none():
    reading = NormalizedReading(
        tenant_id=1,
        plant_id=1,
        device_id=1,
        ts=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    )
    assert reading.power_w is None
    assert reading.energy_day_kwh is None
    assert reading.energy_total_kwh is None
    assert reading.status_raw is None


def test_naive_ts_rejected():
    try:
        NormalizedReading(
            tenant_id=1,
            plant_id=1,
            device_id=1,
            ts=datetime(2026, 7, 19, 12, 0),  # naive -- sem tzinfo
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("ts naive deveria levantar ValidationError")


def test_manufacturer_default_and_device_id_required():
    reading = NormalizedReading(
        tenant_id=1,
        plant_id=1,
        device_id=42,
        ts=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    )
    assert reading.manufacturer == "goodwe"

    try:
        NormalizedReading(
            tenant_id=1,
            plant_id=1,
            ts=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("device_id ausente deveria levantar ValidationError")


def _run_all():
    tests = [
        test_valid_reading_tz_aware_optional_fields_are_none,
        test_naive_ts_rejected,
        test_manufacturer_default_and_device_id_required,
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
