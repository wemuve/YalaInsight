"""Solargis / Global Solar Atlas adapter (interface only).

Global Solar Atlas (World Bank / ESMAP, data by Solargis) offers map and site data under CC BY 4.0 for the
published layers, but programmatic API access and high-resolution time series are commercial Solargis products
requiring a contract and API key. This adapter is a placeholder until a licence is in place.
"""
from __future__ import annotations

from .base import ConnectorError, ResourceEstimate, SolarResourceConnector, SourceMetadata


class SolargisConnector(SolarResourceConnector):
    metadata = SourceMetadata(
        kind="solargis", provider="Solargis / Global Solar Atlas", url="https://solargis.com/", requires_api_key=True,
        license_notes="Commercial API — requires Solargis contract. GSA web layers: CC BY 4.0 with attribution.",
        coverage_notes="Global land coverage between ~60°N and ~50°S (per provider documentation).",
        commercial_use="licence required")

    def estimate(self, lat, lon, kwp, loss_pct, tilt, azimuth) -> ResourceEstimate:  # noqa: ANN001
        raise ConnectorError("Solargis adapter requires a commercial licence and API credentials; not configured.")
