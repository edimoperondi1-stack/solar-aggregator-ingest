"""
SemsClient -- HTTP client for the GoodWe SEMS Portal (legacy CrossLogin).

Only makes HTTP calls and returns raw JSON -- no normalization here (that is
the job of 02-04's connectors/goodwe/connector.py). Endpoints, payloads and
headers below mirror exactly what the disposable spike
(connectors/goodwe/spike.py, plan 02-01) confirmed LIVE against the real
SEMS account -- ver notas internas do projeto (02-SPIKE-FINDINGS.md).

SECURITY: NEVER log the password, the Token header value, or the token blob
(data["data"]). Only status codes / top-level shapes may be logged.
"""

import json
from datetime import date

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

# Confirmed by the 02-01 spike against the live pilot-plant account (see
# 02-SPIKE-FINDINGS.md "LOGIN MODE"). Legacy CrossLogin v2 worked for this
# account -- SEMS+ (semsplus.goodwe.com) was NOT needed. If a future account
# is migrated to SEMS+, swap this constant/authenticate() only -- a contained
# change (ARCHITECTURE.md Pattern 1: auth is isolated behind SemsClient).
LOGIN_URL = "https://www.semsportal.com/api/v2/Common/CrossLogin"
LOGIN_HEADER_TOKEN = '{"version":"v2.0.4","client":"ios","language":"en"}'

# Rate-limit code from research (not observed live in the low-volume 02-01
# spike). Hard stop: never retried via tenacity, caller must abort the run.
RATE_LIMIT_CODE = "GY0429"

# Transport/HTTP errors only -- business-logic failures (bad code/msg in an
# otherwise-200 JSON body) are NOT retried here, they raise immediately.
_RETRYABLE_EXC = (httpx.TransportError, httpx.HTTPStatusError)


class SemsRateLimited(Exception):
    """Raised when SEMS responds with the GY0429 rate-limit code. This is a
    hard stop (per 02-03-PLAN.md constraints) -- the caller should abort the
    run (or cool down ~300s before a fresh run); it is intentionally NOT
    caught by the tenacity retry below."""


class SemsClient:
    """Thin, vendor-specific HTTP client for the SEMS portal. Returns raw
    JSON payloads only -- normalization into the shared DTO happens in
    connectors/goodwe/connector.py (02-04), not here."""

    manufacturer = "goodwe"

    def __init__(self, account: str, password: str, timeout: float = 10.0) -> None:
        self._account = account
        self._password = password
        self._http = httpx.Client(timeout=timeout)
        self.base_url: str | None = None
        self.token: str | None = None

    @retry(
        reraise=True,
        retry=retry_if_exception_type(_RETRYABLE_EXC),
        wait=wait_exponential_jitter(initial=1, max=8),
        stop=stop_after_attempt(4),
    )
    def authenticate(self) -> None:
        """Log in via legacy CrossLogin and capture the REGIONAL base URL
        from the response itself (CONN-01) -- never hardcoded. Intended to be
        called ONCE per run and reused for every subsequent call; re-login
        after this point is reactive only, triggered by _authed_post on a
        token-expiry-shaped response, never a proactive TTL timer (see
        02-03-PLAN.md CONN-01 reconciliation note: none of the 3 reference
        clients track TTL, and a single-plant manual run finishes in well
        under the ~10 min token lifetime)."""
        resp = self._http.post(
            LOGIN_URL,
            headers={"Token": LOGIN_HEADER_TOKEN},
            data={"account": self._account, "pwd": self._password},
        )
        resp.raise_for_status()
        data = resp.json()

        # 02-01 spike: login returns code=0 (int). Normalize with str() anyway
        # since monitor-detail returned it as the STRING '0' (see Task 2).
        if str(data.get("code")) != "0":
            raise RuntimeError(f"SEMS login failed: {data.get('msg')}")

        base_url = data.get("api") or data.get("components", {}).get("api")
        if not base_url:
            raise RuntimeError("SEMS login response missing regional API base URL")

        # data.api observed ending in "/" (02-SPIKE-FINDINGS.md) -- strip so
        # path-joining in _post never produces a double slash.
        self.base_url = base_url.rstrip("/")
        # Full token blob, re-serialized -- sent verbatim as the Token header
        # on every subsequent call. Value is NEVER logged.
        self.token = json.dumps(data["data"])

    def _headers(self) -> dict:
        return {"Token": self.token}

    @retry(
        reraise=True,
        retry=retry_if_exception_type(_RETRYABLE_EXC),
        wait=wait_exponential_jitter(initial=1, max=8),
        stop=stop_after_attempt(4),
    )
    def _post(self, path: str, payload) -> dict:
        """Low-level authenticated POST. Retried via tenacity on
        transport/HTTP errors only -- never a manual retry loop. Empty dict
        payloads are sent form-encoded (matches the spike's
        GetPowerStationIdByOwner call); non-empty dict payloads are sent as
        JSON (matches monitor-detail / intraday)."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        if isinstance(payload, dict) and payload:
            resp = self._http.post(url, headers=self._headers(), json=payload)
        else:
            resp = self._http.post(url, headers=self._headers(), data=payload or {})
        resp.raise_for_status()
        return resp.json()

    def _authed_post(self, path: str, payload) -> dict:
        """Ensures auth (calling authenticate() once if never done), makes
        the call, hard-stops on GY0429, and does ONE reactive re-login if the
        response looks like an expired/invalid token -- then retries the call
        exactly once more before propagating."""
        if self.base_url is None or self.token is None:
            self.authenticate()

        data = self._post(path, payload)

        code = data.get("code")
        if code == RATE_LIMIT_CODE or str(code) == RATE_LIMIT_CODE:
            raise SemsRateLimited(
                f"SEMS rate limit ({RATE_LIMIT_CODE}) on {path} -- hard stop, no retry"
            )

        if self._looks_token_expired(data):
            self.authenticate()
            data = self._post(path, payload)
            if self._looks_token_expired(data):
                raise RuntimeError(
                    f"SEMS re-login did not clear a token error on {path}"
                )

        return data

    @staticmethod
    def _looks_token_expired(data: dict) -> bool:
        """SEMS has no single documented code for 'token expired' across
        endpoints (research: semi-official API). Treat a non-success code
        whose message mentions token/login/session as expired-token --
        distinct from a plain business-logic failure (e.g. bad plant id)."""
        code = data.get("code")
        if code == 0 or str(code) == "0":
            return False
        msg = str(data.get("msg", "")).lower()
        return "token" in msg or "login" in msg or "session" in msg

    # -- CONN-02: list all plants on the account -----------------------------

    def list_plants(self) -> list[dict]:
        """CONN-02: all power stations visible to this SEMS account. Uses
        GetPowerStationIdByOwner -- the endpoint the 02-01 spike marked as
        primary (single call, no pagination observed; see
        02-SPIKE-FINDINGS.md "LIST STATIONS"). Returns the raw list of
        station dicts exactly as SEMS shaped them -- no field renaming/
        normalization here (that's 02-04)."""
        data = self._authed_post("PowerStation/GetPowerStationIdByOwner", {})
        return self._extract_station_list(data.get("data"))

    @staticmethod
    def _extract_station_list(inner) -> list[dict]:
        """The 02-01 spike only observed this endpoint's TOP-LEVEL response
        keys -- the station-list shape itself was not printed live (see
        02-SPIKE-FINDINGS.md "LIST STATIONS" gap note). Handle the common
        SEMS/pygoodwe-reference shapes defensively (a bare list, or a dict
        with the list under 'list'/'powerstationVoList'/'data') rather than
        assuming one -- confirm the exact path on the first live run against
        real plants."""
        if inner is None:
            return []
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            for key in ("list", "powerstationVoList", "data", "powerStationList"):
                candidate = inner.get(key)
                if isinstance(candidate, list):
                    return candidate
        return []

    # -- CONN-03: snapshot / device discovery / raw readings -----------------

    def get_monitor_detail(self, plant_ext_id: str) -> dict:
        """Raw `data` container from GetMonitorDetailByPowerstationId: the
        current snapshot (data.kpi) plus the raw per-inverter list
        (data.inverter). NOTE: this endpoint's `code` came back as the STRING
        '0' in the 02-01 spike (not int 0) -- _authed_post/_looks_token_expired
        already normalize via str(code), so this is handled transparently."""
        response = self._authed_post(
            "v2/PowerStation/GetMonitorDetailByPowerstationId",
            {"powerStationId": plant_ext_id},
        )
        return response.get("data", {})

    def list_devices(self, plant_ext_id: str) -> list[dict]:
        """Raw data.inverter list for a plant. An empty list is the NORMAL
        state for a plant with no inverter linked yet -- confirmed live for
        the pilot plant (Pitfall B) -- never an error/exception. Field
        names (including known SEMS misspellings like 'bettery'/
        'tempperature') are passed through raw for 02-04 to shape."""
        detail = self.get_monitor_detail(plant_ext_id)
        inverters = detail.get("inverter", [])
        return inverters if isinstance(inverters, list) else []

    def fetch_readings(self, plant_ext_id: str, day: date) -> list[dict]:
        """CONN-03 raw readings: the intraday 15-min power curve for one
        calendar day. Source confirmed by the 02-01 spike (decision A in
        02-SPIKE-FINDINGS.md "INTRADAY 15-MIN") -- GetPowerStationPacByDayForApp
        responded code=0 with a data.pacs[] container; the Excel-export
        fallback (B) and snapshot-only fallback (C) are NOT implemented since
        A was confirmed working. Per-point field shapes (date/pac, units,
        naive-local timestamp) could not be observed live (the pilot plant has
        zero generation) -- returned raw and unparsed here; 02-04
        localizes/shapes them. An empty pacs[] (no generation that day) is a
        normal outcome, returned as [] -- never an exception."""
        response = self._authed_post(
            "PowerStationMonitor/GetPowerStationPacByDayForApp",
            {"id": plant_ext_id, "date": day.isoformat()},
        )
        data = response.get("data", {})
        pacs = data.get("pacs", []) if isinstance(data, dict) else []
        return pacs if isinstance(pacs, list) else []
