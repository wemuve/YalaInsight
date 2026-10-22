"""Helpers shared by routers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..core.deps import Principal
from ..models import EnergyReading, Plant


def in_scope(principal: Principal, plant_id: UUID) -> bool:
    """True when the caller may access this plant (per-user plant scope; None = all plants)."""
    return principal.plant_scope is None or plant_id in principal.plant_scope


def scoped(q, column, principal: Principal):  # noqa: ANN001, ANN201
    """Restrict a query to the caller's plant scope."""
    return q if principal.plant_scope is None else q.where(column.in_(principal.plant_scope))


def get_plant(db: Session, plant_id: UUID, principal: Principal) -> Plant:
    plant = db.get(Plant, plant_id)
    # RLS already hides other tenants' rows; the explicit check is defence in depth.
    if plant is None or plant.organization_id != principal.org_id or not in_scope(principal, plant.id):
        raise HTTPException(404, "Plant not found")
    return plant


def resolve_period(db: Session, plant_ids: list[UUID] | None, start: datetime | None, end: datetime | None,
                   default_days: int = 30) -> tuple[datetime, datetime]:
    """Default window = the last ``default_days`` days of available production data (or of today)."""
    if end is None:
        q = select(func.max(EnergyReading.ts))
        if plant_ids is not None:
            q = q.where(EnergyReading.plant_id.in_(plant_ids))
        last = db.scalar(q)
        end = (last + timedelta(hours=1)) if last else datetime.now(UTC)
        end = end.replace(minute=0, second=0, microsecond=0)
        if last and end.hour != 0:
            end = (end + timedelta(days=1)).replace(hour=0)
    if start is None:
        start = end - timedelta(days=default_days)
    for v in (start, end):
        if v.tzinfo is None:
            raise HTTPException(422, "start/end must include a timezone offset, e.g. 2026-01-01T00:00:00Z")
    if end <= start:
        raise HTTPException(422, "end must be after start")
    if end - start > timedelta(days=800):
        raise HTTPException(422, "period too long (max 800 days)")
    return start, end


GEO_SQL = text("""SELECT ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon,
                         ST_AsGeoJSON(boundary)::json AS boundary FROM plants WHERE id = :id""")


def plant_geo(db: Session, plant_id: UUID) -> dict:
    r = db.execute(GEO_SQL, {"id": plant_id}).one()
    return {"lat": r.lat, "lon": r.lon, "boundary": r.boundary}


def wkt_point(lat: float, lon: float) -> str:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "latitude/longitude out of range")
    return f"SRID=4326;POINT({lon} {lat})"


def wkt_polygon(geojson: dict | None) -> str | None:
    """Accept a GeoJSON Polygon (lon, lat order) and return EWKT."""
    if not geojson:
        return None
    if geojson.get("type") != "Polygon" or not geojson.get("coordinates"):
        raise HTTPException(422, "boundary must be a GeoJSON Polygon")
    rings = []
    for ring in geojson["coordinates"]:
        if len(ring) < 4 or ring[0] != ring[-1]:
            raise HTTPException(422, "polygon rings must be closed and have >= 4 positions")
        rings.append("(" + ", ".join(f"{float(x)} {float(y)}" for x, y, *_ in ring) + ")")
    return "SRID=4326;POLYGON(" + ", ".join(rings) + ")"
