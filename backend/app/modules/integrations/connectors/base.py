"""Connector interfaces.

YalaInsight integrates with existing systems instead of replacing them. Each
external system is wrapped in an adapter implementing one of these interfaces.
Every adapter declares licensing and coverage metadata, which is persisted in
``data_sources`` together with retrieval timestamps and model versions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SourceMetadata:
    kind: str
    provider: str
    url: str
    license_notes: str
    coverage_notes: str
    model_version: str | None = None
    requires_api_key: bool = False
    commercial_use: str = "check terms"


@dataclass
class ResourceEstimate:
    """Long-term expected production baseline from a solar resource model."""
    monthly_kwh: dict[int, float]  # month (1-12) -> kWh
    annual_kwh: float
    metadata: SourceMetadata
    retrieved_at: datetime
    inputs: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class TelemetryRecord:
    ts: datetime
    energy_kwh: float
    equipment_ref: str | None = None
    power_kw: float | None = None


class ConnectorError(RuntimeError):
    pass


class SolarResourceConnector(ABC):
    """Solar resource / PV yield model (PVGIS, PVWatts, Solargis, Global Solar Atlas...)."""
    metadata: SourceMetadata

    @abstractmethod
    def estimate(self, lat: float, lon: float, kwp: float, loss_pct: float, tilt: float, azimuth: float) -> ResourceEstimate: ...


class TelemetryConnector(ABC):
    """Operational data source (inverter cloud API, SCADA historian, data logger, smart meter)."""
    metadata: SourceMetadata

    @abstractmethod
    def fetch_energy(self, since: datetime, until: datetime) -> list[TelemetryRecord]: ...


class WeatherConnector(ABC):
    metadata: SourceMetadata

    @abstractmethod
    def fetch_weather(self, lat: float, lon: float, since: datetime, until: datetime) -> list[dict]: ...


class NotConfiguredTelemetry(TelemetryConnector):
    """Placeholder for SCADA / OPC-UA / Modbus / vendor cloud connectors — interface defined, adapter pending."""

    def __init__(self, kind: str, provider: str):
        self.metadata = SourceMetadata(kind, provider, "", "Customer-authorised access required.",
                                       "Plant-specific.", requires_api_key=True)

    def fetch_energy(self, since, until):  # noqa: ANN001
        raise ConnectorError(f"{self.metadata.provider} connector is not implemented yet — use CSV import or the API.")
