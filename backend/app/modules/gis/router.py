"""GeoJSON portfolio layer and PostGIS spatial queries."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ...core.deps import Principal, require, require_module
from ...db import get_db
from ...models import Plant
from ..common import get_plant, in_scope, plant_geo, resolve_period, scoped
from ..dashboard.service import plant_kpis

router = APIRouter(prefix="/gis", tags=["gis"], dependencies=[Depends(require_module("gis"))])


def _feature(geom: dict, props: dict) -> dict:
    return {"type": "Feature", "geometry": geom, "properties": props}


@router.get("/plants")
def plants_geojson(start: datetime | None = None, end: datetime | None = None, with_kpis: bool = True,
                   p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> dict:
    plants = db.scalars(scoped(select(Plant), Plant.id, p)).all()
    s, e = resolve_period(db, [x.id for x in plants] or None, start, end, default_days=30)
    feats = []
    for x in plants:
        g = plant_geo(db, x.id)
        props = plant_kpis(db, x, s, e) if with_kpis else {"plant_id": str(x.id), "name": x.name}
        props["boundary"] = g["boundary"]
        feats.append(_feature({"type": "Point", "coordinates": [g["lon"], g["lat"]]}, props))
    return {"type": "FeatureCollection", "features": feats, "period": {"start": s.isoformat(), "end": e.isoformat()}}


@router.get("/plants/near")
def plants_near(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180), radius_km: float = Query(100, gt=0, le=5000),
                p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    q = text("""SELECT id, name, code, dc_capacity_kwp,
                       ST_Distance(location, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) / 1000 AS distance_km
                FROM plants WHERE ST_DWithin(location, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r)
                ORDER BY distance_km""")
    return [{"plant_id": str(r.id), "name": r.name, "code": r.code, "dc_capacity_kwp": r.dc_capacity_kwp,
             "distance_km": round(r.distance_km, 2)} for r in db.execute(q, {"lat": lat, "lon": lon, "r": radius_km * 1000}) if in_scope(p, r.id)]


@router.get("/plants/within")
def plants_within(bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
                  p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    a, b, c, d = (float(v) for v in bbox.split(","))
    q = text("""SELECT id, name, code FROM plants
                WHERE ST_Intersects(location, ST_MakeEnvelope(:a, :b, :c, :d, 4326)::geography)""")
    return [{"plant_id": str(r.id), "name": r.name, "code": r.code} for r in db.execute(q, {"a": a, "b": b, "c": c, "d": d}) if in_scope(p, r.id)]


@router.get("/regions")
def regions(start: datetime | None = None, end: datetime | None = None, p: Principal = Depends(require("plants:read")),
            db: Session = Depends(get_db)) -> list[dict]:
    """Regional portfolio comparison grouped by country and region."""
    plants = db.scalars(scoped(select(Plant), Plant.id, p)).all()
    s, e = resolve_period(db, [x.id for x in plants] or None, start, end, default_days=30)
    groups: dict[tuple, dict] = {}
    for x in plants:
        k = plant_kpis(db, x, s, e)
        g = groups.setdefault((x.country_code, x.region), {"country_code": x.country_code, "region": x.region, "plants": 0,
                                                           "dc_capacity_kwp": 0.0, "actual_kwh": 0.0, "expected_kwh": 0.0,
                                                           "addressable_value": 0.0, "active_alerts": 0, "currencies": set()})
        g["plants"] += 1
        g["dc_capacity_kwp"] += x.dc_capacity_kwp
        g["actual_kwh"] += k["performance"]["actual_kwh_compared"]
        g["expected_kwh"] += k["performance"]["expected_kwh"]
        g["active_alerts"] += k["alerts"]["active"]
        if k["loss"]:
            g["addressable_value"] += k["loss"]["addressable_value"]
            g["currencies"].add(k["loss"]["currency"])
    out = []
    for g in groups.values():
        g["performance_index"] = g["actual_kwh"] / g["expected_kwh"] if g["expected_kwh"] else None
        g["currencies"] = sorted(g["currencies"])
        out.append(g)
    return sorted(out, key=lambda g: (g["country_code"] or "", g["region"] or ""))


@router.get("/plants/{plant_id}/assets")
def plant_assets(plant_id: UUID, p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    g = plant_geo(db, plant.id)
    feats = [_feature({"type": "Point", "coordinates": [g["lon"], g["lat"]]}, {"kind": "plant", "name": plant.name})]
    if g["boundary"]:
        feats.append(_feature(g["boundary"], {"kind": "boundary", "name": plant.name}))
    q = text("""SELECT id, name, kind, serial_number, status, ST_AsGeoJSON(location)::json AS geom FROM equipment
                WHERE plant_id = :p AND location IS NOT NULL""")
    for r in db.execute(q, {"p": plant.id}):
        feats.append(_feature(r.geom, {"kind": r.kind, "name": r.name, "equipment_id": str(r.id), "serial_number": r.serial_number,
                                       "status": r.status}))
    return {"type": "FeatureCollection", "features": feats}
