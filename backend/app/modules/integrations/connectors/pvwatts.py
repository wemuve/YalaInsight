"""NREL PVWatts v8 adapter.

Docs: https://developer.nrel.gov/docs/solar/pvwatts/v8/
* Requires an NREL developer API key (env NREL_API_KEY); rate-limited per key.
* Uses NSRDB where available (Americas, parts of Asia/Africa via NSRDB international datasets) — coverage
  must be checked per site; returns an error for unsupported locations.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ....config import settings
from .base import ConnectorError, ResourceEstimate, SolarResourceConnector, SourceMetadata

API = "https://developer.nrel.gov/api/pvwatts/v8.json"


class PVWattsConnector(SolarResourceConnector):
    metadata = SourceMetadata(
        kind="pvwatts", provider="NREL PVWatts", url=API, model_version="PVWatts v8", requires_api_key=True,
        license_notes="Requires NREL API key; subject to NREL API terms and rate limits.",
        coverage_notes="NSRDB coverage; not global. Verify dataset availability for each site.")

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or settings.pvwatts_api_key
        self.client = client or httpx.Client(timeout=settings.external_http_timeout)

    def estimate(self, lat, lon, kwp, loss_pct, tilt, azimuth) -> ResourceEstimate:  # noqa: ANN001
        if not self.api_key:
            raise ConnectorError("NREL_API_KEY is not configured")
        # PVWatts azimuth: 180 = south. PVGIS/Yala aspect: 0 = equator-facing; convert for the hemisphere.
        az = (180 + azimuth) % 360 if lat >= 0 else (0 + azimuth) % 360
        p = {"api_key": self.api_key, "lat": lat, "lon": lon, "system_capacity": kwp, "losses": loss_pct,
             "tilt": tilt, "azimuth": az, "array_type": 0, "module_type": 0, "dataset": "intl" if not -170 < lon < -50 else "nsrdb"}
        try:
            r = self.client.get(API, params=p)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"PVWatts request failed: {exc}") from exc
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or data.get("errors"):
            raise ConnectorError(f"PVWatts error {r.status_code}: {data.get('errors') or r.text[:300]}")
        ac = data["outputs"]["ac_monthly"]
        p.pop("api_key")
        return ResourceEstimate({i + 1: float(v) for i, v in enumerate(ac)}, float(data["outputs"]["ac_annual"]),
                                self.metadata, datetime.now(UTC), p, data.get("station_info", {}))
