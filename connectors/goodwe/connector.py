"""connectors/goodwe/connector.py -- GoodWeConnector: the other side of the
CONN-04 seam.

connectors/goodwe/client.py (02-03) only speaks HTTP and returns RAW JSON --
no normalization there. This module takes that raw JSON and produces the
single cross-vendor DTO (`connectors.base.NormalizedReading`), plus device
discovery (D-06) with the synthetic-device fallback (D-07).

The connector does NOT open its own DB connection or HTTP client -- it
orchestrates an injected `SemsClient` (constructor) and an injected DB
cursor (`discover_devices`, passed in by the ingestion CLI, plan 02-05).
This keeps the connector trivially testable (no live SEMS account or
Postgres needed for the tests in connector_test.py) and keeps commit/
rollback control with the CLI.

Timezone (Pitfall E, 02-RESEARCH.md): SEMS returns each intraday point's
timestamp as NAIVE LOCAL TIME -- no offset in the payload. `_localize`
always localizes with the PLANT's own IANA zone name (read from
`plants.timezone`, e.g. "America/Cuiaba" for the pilot plant, UTC-4) via
`zoneinfo`, never a hardcoded `timedelta`. The pilot plant is a full hour off
from the more common "America/Sao_Paulo" (UTC-3) -- hardcoding either offset
silently corrupts every reading for the other zone.

Field-shape confidence (see 02-SPIKE-FINDINGS.md "INTRADAY 15-MIN" and
02-RESEARCH.md Pitfall C): the pilot plant is offline with zero
generation, so `data.pacs[]` came back empty in the live spike -- the
per-point field names below (`date`/`pac`, and any energy/status fields)
are MEDIUM-confidence hypotheses derived from the pygoodwe reference
implementation, not a field-for-field live-observed contract. Extraction
below is intentionally defensive (`_first_present` over a few plausible
aliases, missing -> None, never invented 0) so an empty window or a
slightly different-than-expected point shape degrades gracefully instead
of raising. The exact raw shape MUST be logged/confirmed the first time
this runs against a plant that is actually generating.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from connectors.base import NormalizedReading
from connectors.common.db import resolve_plant_device
from connectors.goodwe.client import SemsClient

# Format observed/hypothesized for SEMS's naive local timestamps (02-01 spike
# + 02-RESEARCH.md "Zoneinfo localization"). Confirm against a live,
# generating plant on first real run.
_SEMS_NAIVE_TS_FORMAT = "%m/%d/%Y %H:%M:%S"

_UTC = ZoneInfo("UTC")


def _first_present(point: dict, *keys: str):
    """Returns the first non-None value found among `keys` in `point`, or
    None if none are present. Used for defensive field extraction against a
    per-point shape that was NOT observed live (pilot plant offline) -- see
    module docstring. Never invents a 0/empty-string default; absence stays
    None."""
    for key in keys:
        value = point.get(key)
        if value is not None:
            return value
    return None


def _to_float(value) -> float | None:
    """Coerces a raw numeric-ish value to float, or None if missing/
    unparseable. Missing data must stay None (never 0) -- the pilot plant's
    real offline state is semantically different from a real 0 W
    measurement."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_status_raw(value) -> str | None:
    """Preserves the vendor's raw status code/text as-is (as a string) --
    D-08: this phase only preserves status_raw, it never maps/interprets
    vendor status codes (that is Phase 4's job)."""
    if value is None:
        return None
    return str(value)


class GoodWeConnector:
    """Implements the `Connector` Protocol (connectors/base.py) for GoodWe's
    SEMS portal. Delegates all HTTP to the injected `SemsClient` (02-03) and
    all DB writes to the injected cursor + connectors/common/db.py helpers
    (02-02) -- this class only orchestrates and normalizes."""

    manufacturer = "goodwe"

    def __init__(self, client: SemsClient) -> None:
        self._client = client

    # -- Connector Protocol: thin delegation to the injected client ---------

    def authenticate(self) -> None:
        self._client.authenticate()

    def list_plants(self) -> list[dict]:
        return self._client.list_plants()

    def list_devices(self, plant_ext_id: str) -> list[dict]:
        return self._client.list_devices(plant_ext_id)

    def fetch_readings(self, plant_ext_id: str, day: date) -> list[dict]:
        return self._client.fetch_readings(plant_ext_id, day)

    # -- Timezone localization (Pitfall E) ------------------------------------

    @staticmethod
    def _localize(naive_str: str, plant_tz: str) -> datetime:
        """Parses a SEMS naive-local timestamp string and localizes it with
        the PLANT's own IANA timezone name, then converts to UTC.

        Never hardcodes an offset (no `timedelta(hours=3|4)`) -- always use
        the named zone via `zoneinfo.ZoneInfo(plant_tz)` so this stays
        correct per-plant (the pilot plant is America/Cuiaba, UTC-4; a future
        America/Sao_Paulo plant is UTC-3) and automatically correct if
        Brazil's DST policy ever changes again.
        """
        naive = datetime.strptime(naive_str, _SEMS_NAIVE_TS_FORMAT)
        local = naive.replace(tzinfo=ZoneInfo(plant_tz))
        return local.astimezone(_UTC)

    # -- CONN-04: raw SEMS JSON -> NormalizedReading[] ------------------------

    def normalize(
        self,
        raw: list[dict],
        tenant_id: int,
        plant_id: int,
        device_id: int,
        plant_tz: str,
    ) -> list[NormalizedReading]:
        """Turns the raw intraday points (`SemsClient.fetch_readings`'s
        `data.pacs[]`) into the shared `NormalizedReading` DTO.

        - `raw` empty (the pilot plant's normal offline state, or any day
          with no generation) -> returns `[]` cleanly, never raises.
        - Each point's naive local timestamp is localized via `_localize`
          using `plant_tz` (never a hardcoded offset).
        - Missing power/energy/status fields become `None`, never an
          invented `0` -- see `_first_present`/`_to_float`.
        - `status_raw` is preserved as raw text, never mapped/interpreted
          (D-08 -- Phase 4's job).
        """
        readings: list[NormalizedReading] = []
        for point in raw or []:
            ts_raw = point.get("date")
            if not ts_raw:
                # No usable timestamp on this point -- skip defensively
                # rather than raise; an unusable point should not abort the
                # whole window.
                continue
            ts = self._localize(ts_raw, plant_tz)
            readings.append(
                NormalizedReading(
                    tenant_id=tenant_id,
                    plant_id=plant_id,
                    device_id=device_id,
                    manufacturer=self.manufacturer,
                    ts=ts,
                    power_w=_to_float(_first_present(point, "pac", "power")),
                    energy_day_kwh=_to_float(
                        _first_present(
                            point, "energy_day_kwh", "eday", "e_today", "today_energy"
                        )
                    ),
                    energy_total_kwh=_to_float(
                        _first_present(point, "energy_total_kwh", "etotal", "e_total")
                    ),
                    status_raw=_to_status_raw(
                        _first_present(point, "status", "status_raw")
                    ),
                )
            )
        return readings

    # -- D-06/D-07: device discovery + synthetic fallback ---------------------

    def discover_devices(
        self,
        cur,
        tenant_id: int,
        plant_id: int,
        external_plant_id: str,
    ) -> list[tuple[int, dict | None]]:
        """Resolves the device_id(s) this plant's readings should attach to.

        An empty inverter list is the NORMAL state for the pilot plant
        (offline, no inverter linked yet) -- Pitfall B -- never an error;
        `resolve_plant_device` (connectors/common/db.py, 02-02) already
        handles the synthetic `plant` device fallback (D-07) and the
        idempotent upsert for real inverters (D-06). This method does not
        duplicate that logic -- it only fetches the raw inverter list (via
        the injected client) and delegates the resolve/upsert to
        common/db.py. `cur` is injected by the CLI (02-05); this class never
        opens its own DB connection.

        Never returns an empty list -- `readings.device_id` is NOT NULL.
        """
        inverters = self.list_devices(external_plant_id)
        return resolve_plant_device(
            cur,
            tenant_id,
            plant_id,
            self.manufacturer,
            external_plant_id,
            inverters,
        )
