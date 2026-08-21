"""ingest/aggregate_test.py -- proves the pure compute layer of
ingest/aggregate.py (plan 04-02, Task 1).

Covers the 5 <behavior> cases from 04-02-PLAN.md (all pure, no DB):
  1. energy_kwh_from_power_trapezoid([t0, t0+1h, t0+2h], [0, 1000, 0]) returns
     ~1.0 kWh (trapezoid of watts over hours / 1000); uses numpy.trapezoid
     (NOT numpy.trapz)
  2. energy_kwh_from_power_trapezoid with <2 points returns 0.0
  3. plant_day_energy_kwh: given per-device max energy {devA:5.0, devB:3.0}
     returns 8.0 (sum across devices, D-06)
  4. plant_day_energy_kwh: a device whose MAX(energy_day_kwh) is None uses
     its trapezoidal fallback value instead of dropping the device (D-07)
  5. recompute_window_days(local_today, 4) returns
     [today, today-1, today-2, today-3] (D-15: today + last 3)

Usage: `python -m pytest ingest/aggregate_test.py` or
`python ingest/aggregate_test.py` (direct-script fallback, no pytest
required).
"""

import os
import sys
from datetime import date, datetime, timedelta

# Allows running this file directly (`python ingest/aggregate_test.py`) as
# well as via `python -m pytest` -- makes the `ingest` package importable when
# sys.path[0] would otherwise be ingest/, not the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.aggregate import (
    energy_kwh_from_power_trapezoid,
    plant_day_energy_kwh,
    recompute_window_days,
)


def test_energy_kwh_from_power_trapezoid_basic_triangle_returns_one_kwh():
    t0 = datetime(2026, 7, 1, 12, 0, 0)
    timestamps = [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)]
    power_w = [0.0, 1000.0, 0.0]
    result = energy_kwh_from_power_trapezoid(timestamps, power_w)
    assert abs(result - 1.0) < 1e-9


def test_energy_kwh_from_power_trapezoid_fewer_than_two_points_returns_zero():
    t0 = datetime(2026, 7, 1, 12, 0, 0)
    assert energy_kwh_from_power_trapezoid([], []) == 0.0
    assert energy_kwh_from_power_trapezoid([t0], [500.0]) == 0.0


def test_plant_day_energy_kwh_sums_across_devices():
    device_max_energy_kwh = {"devA": 5.0, "devB": 3.0}
    assert plant_day_energy_kwh(device_max_energy_kwh) == 8.0


def test_plant_day_energy_kwh_none_device_uses_trapezoid_fallback_not_dropped():
    device_max_energy_kwh = {"devA": 5.0, "devB": None}
    device_fallback_kwh = {"devB": 2.5}
    result = plant_day_energy_kwh(device_max_energy_kwh, device_fallback_kwh)
    assert result == 7.5  # 5.0 + 2.5 -- devB NOT dropped, fallback used


def test_recompute_window_days_default_four_returns_today_and_last_three():
    today = date(2026, 7, 21)
    days = recompute_window_days(today, 4)
    assert days == [
        today,
        today - timedelta(days=1),
        today - timedelta(days=2),
        today - timedelta(days=3),
    ]


def _run_all():
    tests = [
        test_energy_kwh_from_power_trapezoid_basic_triangle_returns_one_kwh,
        test_energy_kwh_from_power_trapezoid_fewer_than_two_points_returns_zero,
        test_plant_day_energy_kwh_sums_across_devices,
        test_plant_day_energy_kwh_none_device_uses_trapezoid_fallback_not_dropped,
        test_recompute_window_days_default_four_returns_today_and_last_three,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("AGGREGATE OK")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
