from __future__ import annotations

from .base import NotConfiguredTelemetry, SolarResourceConnector
from .pvgis import PVGISConnector
from .pvwatts import PVWattsConnector
from .solargis import SolargisConnector

RESOURCE_CONNECTORS: dict[str, type[SolarResourceConnector]] = {
    "pvgis": PVGISConnector, "pvwatts": PVWattsConnector, "solargis": SolargisConnector,
}

TELEMETRY_PLANNED = {
    "scada_opcua": NotConfiguredTelemetry("scada", "SCADA (OPC-UA)"),
    "modbus_logger": NotConfiguredTelemetry("data_logger", "Data logger (Modbus TCP)"),
    "sma_ennexos": NotConfiguredTelemetry("inverter_api", "SMA ennexOS / Sunny Portal"),
    "huawei_fusionsolar": NotConfiguredTelemetry("inverter_api", "Huawei FusionSolar Northbound API"),
    "sungrow_isolarcloud": NotConfiguredTelemetry("inverter_api", "Sungrow iSolarCloud"),
    "smart_meter_dlms": NotConfiguredTelemetry("smart_meter", "Smart meter (DLMS/COSEM)"),
    "cmms": NotConfiguredTelemetry("asset_management", "Asset management / CMMS"),
}


def catalog() -> list[dict]:
    out = []
    for key, cls in RESOURCE_CONNECTORS.items():
        m = cls.metadata
        out.append({"key": key, "type": "solar_resource", "status": "available" if key != "solargis" else "licence_required",
                    **m.__dict__})
    out.append({"key": "csv", "type": "telemetry", "status": "available", "kind": "csv_upload", "provider": "CSV upload / API",
                "license_notes": "Customer data", "coverage_notes": "Any plant", "url": "/api/v1/plants/{id}/imports"})
    for key, c in TELEMETRY_PLANNED.items():
        out.append({"key": key, "type": "telemetry", "status": "interface_defined", **c.metadata.__dict__})
    return out
