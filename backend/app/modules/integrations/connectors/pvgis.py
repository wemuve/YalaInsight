"""PVGIS (European Commission JRC) adapter — documented non-interactive API, v5.3.

Docs: https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/getting-started-pvgis/api-non-interactive-service_en
* Free, no key. The JRC asks users to respect a rate limit (currently 30 calls/s per IP) — we cache results.
* Coverage depends on the radiation database (PVGIS-SARAH3 covers Europe, Africa and much of Asia; ERA5
  is used elsewhere). Results are long-term typical estimates, not measurements of actual plant condition.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ....config import settings
from .base import ConnectorError, ResourceEstimate, SolarResourceConnector, SourceMetadata

API = "https://re.jrc.ec.europa.eu/api/v5_3/PVcalc"


class PVGISConnector(SolarResourceConnector):
    metadata = SourceMetadata(
        kind="pvgis", provider="European Commission JRC — PVGIS", url=API, model_version="PVGIS 5.3",
        license_notes="Free to use; cite PVGIS / JRC. No warranty. Respect JRC API rate limits.",
        coverage_notes="Depends on radiation database (SARAH3: Europe, Africa, most of Asia; ERA5 elsewhere). "
                       "Not available for all latitudes/oceans.",
        commercial_use="permitted with attribution (check current JRC terms)")

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=settings.external_http_timeout)

    def params(self, lat, lon, kwp, loss_pct, tilt, azimuth) -> dict:  # noqa: ANN001
        return {"lat": lat, "lon": lon, "peakpower": kwp, "loss": loss_pct, "angle": tilt, "aspect": azimuth,
                "outputformat": "json", "pvtechchoice": "crystSi", "mountingplace": "free"}

    def estimate(self, lat, lon, kwp, loss_pct, tilt, azimuth) -> ResourceEstimate:  # noqa: ANN001
        p = self.params(lat, lon, kwp, loss_pct, tilt, azimuth)
        try:
            r = self.client.get(API, params=p)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"PVGIS request failed: {exc}") from exc
        if r.status_code != 200:
            raise ConnectorError(f"PVGIS returned {r.status_code}: {r.text[:300]}")
        return self.parse(r.json(), p)

    def parse(self, data: dict, inputs: dict) -> ResourceEstimate:
        try:
            monthly = {int(m["month"]): float(m["E_m"]) for m in data["outputs"]["monthly"]["fixed"]}
            annual = float(data["outputs"]["totals"]["fixed"]["E_y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectorError(f"Unexpected PVGIS response structure: {exc}") from exc
        meta = self.metadata
        db_name = data.get("inputs", {}).get("meteo_data", {}).get("radiation_db")
        if db_name:
            meta = SourceMetadata(**{**meta.__dict__, "model_version": f"PVGIS 5.3 / {db_name}"})
        return ResourceEstimate(monthly, annual, meta, datetime.now(UTC), inputs, data.get("inputs", {}))
