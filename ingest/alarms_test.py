"""ingest/alarms_test.py -- prova os predicados de deteccao + constantes de
limiar do plano 04-03, Task 1 (METR-03).

Cobre os 6 casos de <behavior> do 04-03-PLAN.md:
  1. is_offline(None, now_utc) -> True (nunca recebeu leitura)
  2. is_offline(now-3h, now_utc) -> True (>= OFFLINE_THRESHOLD de 2h)
  3. is_offline(now-30min, now_utc) -> False (< 2h)
  4. is_zero_generation com >=4 valores quase-zero consecutivos -> True
  5. is_zero_generation com a maior sequencia quase-zero == 3 -> False
  6. ZERO_GEN_WATTS == 50 e OFFLINE_THRESHOLD == timedelta(hours=2)

Usage: `python -m pytest ingest/alarms_test.py` ou `python ingest/alarms_test.py`
(fallback de script direto, sem exigir pytest).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Permite rodar este arquivo diretamente (`python ingest/alarms_test.py`) e
# via `python -m pytest` -- torna o pacote `ingest` importavel quando
# sys.path[0] seria ingest/, nao a raiz do repo (mesmo padrao de
# ingest/run_test.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.alarms import (
    OFFLINE_THRESHOLD,
    ZERO_GEN_WATTS,
    is_offline,
    is_zero_generation,
)

NOW = datetime(2026, 7, 21, 15, 0, 0, tzinfo=timezone.utc)


def test_is_offline_none_watermark_returns_true():
    assert is_offline(None, NOW) is True


def test_is_offline_three_hours_ago_returns_true():
    assert is_offline(NOW - timedelta(hours=3), NOW) is True


def test_is_offline_thirty_minutes_ago_returns_false():
    assert is_offline(NOW - timedelta(minutes=30), NOW) is False


def test_is_zero_generation_four_consecutive_near_zero_returns_true():
    values = [500.0, 40.0, 30.0, 20.0, 10.0, 600.0]
    assert is_zero_generation(values) is True


def test_is_zero_generation_longest_run_three_returns_false():
    values = [500.0, 40.0, 30.0, 20.0, 600.0, 10.0, 5.0, 0.0, 700.0]
    assert is_zero_generation(values) is False


def test_threshold_constants_documented_values():
    assert ZERO_GEN_WATTS == 50
    assert OFFLINE_THRESHOLD == timedelta(hours=2)


def _run_all():
    tests = [
        test_is_offline_none_watermark_returns_true,
        test_is_offline_three_hours_ago_returns_true,
        test_is_offline_thirty_minutes_ago_returns_false,
        test_is_zero_generation_four_consecutive_near_zero_returns_true,
        test_is_zero_generation_longest_run_three_returns_false,
        test_threshold_constants_documented_values,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("ALARMS OK")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
