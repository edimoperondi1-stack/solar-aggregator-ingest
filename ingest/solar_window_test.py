"""ingest/solar_window_test.py -- proves the coord lookup + pvlib solar-window
contract (plan 04-01, Task 3).

Covers the 5 <behavior> cases from 04-01-PLAN.md:
  1. load_plant_coords() returns a dict with both known external_plant_id keys
     mapping to (lat, lon) float tuples
  2. get_plant_coords("147493") returns (-11.86, -55.50) -- the pilot plant's
     real SunWEG external_plant_id since Phase 6 migration 011 rekeyed
     config/plant_coordinates.toml away from the old (wrong) SEMS UUID
  3. get_plant_coords("does-not-exist") returns None (graceful -- no exception)
  4. solar_window_utc(-11.86, -55.50, "America/Cuiaba", date(2026,7,1)) returns
     two datetimes; both have tzinfo == UTC; sunrise < sunset
  5. solar_window_utc with margin_hours=1.0 yields a strictly NARROWER window
     than margin_hours=0.0 (later sunrise, earlier sunset) for the same inputs

NOTE: PLANT_A_ID and PLANT_B_ID below are read against the REAL
config/plant_coordinates.toml (this test is not mocked), so they intentionally
match the functional keys that file uses in production, not arbitrary test
values -- ver README para o motivo dessas duas chaves serem mantidas.

Usage: `python -m pytest ingest/solar_window_test.py` or
`python ingest/solar_window_test.py` (direct-script fallback, no pytest
required).
"""

import os
import sys
from datetime import date, timezone

# Allows running this file directly (`python ingest/solar_window_test.py`) as
# well as via `python -m pytest` -- makes the `ingest` package importable when
# sys.path[0] would otherwise be ingest/, not the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.solar_window import get_plant_coords, load_plant_coords, solar_window_utc

PLANT_A_ID = "147493"  # SunWEG id, rekeyed by Phase 6 migration 011 (was the wrong SEMS UUID)
PLANT_B_ID = "236b16f4-8a64-4738-ad0b-7fd4e2055c37"
CUIABA = "America/Cuiaba"  # both plants -- UTC-4, no DST


def test_load_plant_coords_returns_both_known_plants():
    coords = load_plant_coords()
    assert PLANT_A_ID in coords
    assert PLANT_B_ID in coords
    for lat, lon in coords.values():
        assert isinstance(lat, float)
        assert isinstance(lon, float)


def test_get_plant_coords_returns_pilot_lat_lon():
    assert get_plant_coords(PLANT_A_ID) == (-11.86, -55.50)


def test_get_plant_coords_missing_plant_returns_none_not_exception():
    assert get_plant_coords("does-not-exist") is None


def test_solar_window_utc_returns_utc_aware_sunrise_before_sunset():
    sunrise, sunset = solar_window_utc(-11.86, -55.50, CUIABA, date(2026, 7, 1))
    assert sunrise.tzinfo is not None
    assert sunset.tzinfo is not None
    assert sunrise.utcoffset() == timezone.utc.utcoffset(None)
    assert sunset.utcoffset() == timezone.utc.utcoffset(None)
    assert sunrise < sunset


def test_solar_window_utc_margin_narrows_window():
    day = date(2026, 7, 1)
    sunrise0, sunset0 = solar_window_utc(-11.86, -55.50, CUIABA, day, margin_hours=0.0)
    sunrise1, sunset1 = solar_window_utc(-11.86, -55.50, CUIABA, day, margin_hours=1.0)
    assert sunrise1 > sunrise0
    assert sunset1 < sunset0


def _run_all():
    tests = [
        test_load_plant_coords_returns_both_known_plants,
        test_get_plant_coords_returns_pilot_lat_lon,
        test_get_plant_coords_missing_plant_returns_none_not_exception,
        test_solar_window_utc_returns_utc_aware_sunrise_before_sunset,
        test_solar_window_utc_margin_narrows_window,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("SOLAR_WINDOW OK")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
