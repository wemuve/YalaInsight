"""Plants, equipment register, energy contracts, operational events and exchange rates."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.audit import audit
from ...core.deps import Principal, client_ip, require
from ...db import get_db
from ...models import (
    DataSource,
    EmissionFactor,
    EnergyContract,
    Equipment,
    ExchangeRate,
    OperationalEvent,
    Organization,
    Plant,
    SubscriptionPlan,
)
from ..common import get_plant, plant_geo, wkt_point, wkt_polygon

router = APIRouter(tags=["plants"])

PlantStatus = Literal["construction", "commissioning", "operational", "decommissioned"]


class PlantIn(BaseModel):
    name: str = Field(max_length=200)
    code: str = Field(max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    technology: str = "mono-Si"
    mounting: str = "fixed"
    dc_capacity_kwp: float = Field(gt=0, le=5_000_000)
    ac_capacity_kw: float = Field(gt=0, le=5_000_000)
    commissioning_date: date | None = None
    status: PlantStatus = "operational"
    country_code: str | None = Field(None, pattern="^[A-Z]{2}$")
    region: str | None = None
    timezone: str = "UTC"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    boundary: dict | None = None
    tilt_deg: float = Field(20, ge=0, le=90)
    azimuth_deg: float = Field(0, ge=-180, le=180)
    temp_coeff_pct_per_c: float = Field(-0.37, ge=-1, le=0)
    noct_c: float = Field(45, ge=30, le=60)
    system_loss_pct: float = Field(14, ge=0, le=50)
    annual_degradation_pct: float = Field(0.5, ge=0, le=5)
    expected_pr: float = Field(0.8, gt=0, le=1)
    currency: str = Field("USD", pattern="^[A-Z]{3}$")
    emission_factor_id: UUID | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown IANA timezone") from None
        return v


class PlantPatch(BaseModel):
    model_config = {"extra": "forbid"}
    name: str | None = None
    technology: str | None = None
    mounting: str | None = None
    dc_capacity_kwp: float | None = Field(None, gt=0)
    ac_capacity_kw: float | None = Field(None, gt=0)
    commissioning_date: date | None = None
    status: PlantStatus | None = None
    region: str | None = None
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    boundary: dict | None = None
    tilt_deg: float | None = None
    azimuth_deg: float | None = None
    temp_coeff_pct_per_c: float | None = None
    system_loss_pct: float | None = None
    annual_degradation_pct: float | None = None
    expected_pr: float | None = None
    emission_factor_id: UUID | None = None


def plant_out(db: Session, p: Plant) -> dict:
    geo = plant_geo(db, p.id)
    return {"id": str(p.id), "name": p.name, "code": p.code, "technology": p.technology, "mounting": p.mounting,
            "dc_capacity_kwp": p.dc_capacity_kwp, "ac_capacity_kw": p.ac_capacity_kw,
            "commissioning_date": p.commissioning_date.isoformat() if p.commissioning_date else None, "status": p.status,
            "country_code": p.country_code, "region": p.region, "timezone": p.timezone, "latitude": geo["lat"],
            "longitude": geo["lon"], "boundary": geo["boundary"], "tilt_deg": p.tilt_deg, "azimuth_deg": p.azimuth_deg,
            "temp_coeff_pct_per_c": p.temp_coeff_pct_per_c, "noct_c": p.noct_c, "system_loss_pct": p.system_loss_pct,
            "annual_degradation_pct": p.annual_degradation_pct, "expected_pr": p.expected_pr, "currency": p.currency,
            "emission_factor_id": str(p.emission_factor_id) if p.emission_factor_id else None}


@router.get("/plants")
def list_plants(p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    return [plant_out(db, x) for x in db.scalars(select(Plant).order_by(Plant.name))]


@router.post("/plants", status_code=201)
def create_plant(body: PlantIn, request: Request, p: Principal = Depends(require("plants:write")), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, p.org_id)
    plan = db.get(SubscriptionPlan, org.plan_code)
    count = db.scalar(select(func.count(Plant.id)))
    if plan.max_plants is not None and count >= plan.max_plants:
        raise HTTPException(402, f"Plan '{plan.name}' allows {plan.max_plants} plant(s). Upgrade to add more.")
    if body.emission_factor_id and not db.get(EmissionFactor, body.emission_factor_id):
        raise HTTPException(422, "unknown emission factor")
    data = body.model_dump(exclude={"latitude", "longitude", "boundary"})
    plant = Plant(organization_id=p.org_id, location=wkt_point(body.latitude, body.longitude),
                  boundary=wkt_polygon(body.boundary), **data)
    db.add(plant)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A plant with this code already exists") from None
    audit(db, org_id=p.org_id, user_id=p.user_id, action="plant.create", entity_type="plant", entity_id=plant.id,
          details={"name": plant.name, "dc_kwp": plant.dc_capacity_kwp}, ip=client_ip(request))
    db.commit()
    return plant_out(db, plant)


@router.get("/plants/{plant_id}")
def read_plant(plant_id: UUID, p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> dict:
    return plant_out(db, get_plant(db, plant_id, p))


@router.patch("/plants/{plant_id}")
def update_plant(plant_id: UUID, body: PlantPatch, request: Request, p: Principal = Depends(require("plants:write")),
                 db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    changes = body.model_dump(exclude_unset=True)
    lat, lon = changes.pop("latitude", None), changes.pop("longitude", None)
    if (lat is None) != (lon is None):
        raise HTTPException(422, "latitude and longitude must be updated together")
    if lat is not None:
        plant.location = wkt_point(lat, lon)
    if "boundary" in changes:
        plant.boundary = wkt_polygon(changes.pop("boundary"))
    for k, v in changes.items():
        setattr(plant, k, v)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="plant.update", entity_type="plant", entity_id=plant.id,
          details={k: str(v) for k, v in body.model_dump(exclude_unset=True).items()}, ip=client_ip(request))
    db.commit()
    return plant_out(db, plant)


@router.delete("/plants/{plant_id}", status_code=204)
def delete_plant(plant_id: UUID, request: Request, p: Principal = Depends(require("plants:write")), db: Session = Depends(get_db)) -> None:
    plant = get_plant(db, plant_id, p)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="plant.delete", entity_type="plant", entity_id=plant.id,
          details={"name": plant.name}, ip=client_ip(request))
    db.delete(plant)
    db.commit()


# --------------------------------------------------------------------------- equipment


class EquipmentIn(BaseModel):
    kind: Literal["inverter", "string", "combiner", "meter", "weather_station", "transformer", "tracker", "sensor", "module_block"]
    name: str = Field(max_length=200)
    parent_id: UUID | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    rated_power_kw: float | None = Field(None, ge=0)
    dc_capacity_kwp: float | None = Field(None, ge=0)
    commissioning_date: date | None = None
    warranty_end: date | None = None
    warranty_terms: str | None = None
    status: Literal["in_service", "faulty", "under_repair", "retired"] = "in_service"
    latitude: float | None = None
    longitude: float | None = None


EQ_GEO = text("SELECT id, ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon FROM equipment WHERE plant_id = :p")


def equipment_out(e: Equipment, geo: dict | None = None) -> dict:
    lat, lon = (geo or {}).get(e.id, (None, None))
    today = date.today()
    return {"id": str(e.id), "plant_id": str(e.plant_id), "parent_id": str(e.parent_id) if e.parent_id else None,
            "kind": e.kind, "name": e.name, "manufacturer": e.manufacturer, "model": e.model, "serial_number": e.serial_number,
            "rated_power_kw": e.rated_power_kw, "dc_capacity_kwp": e.dc_capacity_kwp,
            "commissioning_date": e.commissioning_date.isoformat() if e.commissioning_date else None,
            "warranty_end": e.warranty_end.isoformat() if e.warranty_end else None,
            "warranty_active": (e.warranty_end >= today) if e.warranty_end else None, "warranty_terms": e.warranty_terms,
            "status": e.status, "latitude": lat, "longitude": lon}


@router.get("/plants/{plant_id}/equipment")
def list_equipment(plant_id: UUID, p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    get_plant(db, plant_id, p)
    geo = {r.id: (r.lat, r.lon) for r in db.execute(EQ_GEO, {"p": plant_id})}
    return [equipment_out(e, geo) for e in db.scalars(select(Equipment).where(Equipment.plant_id == plant_id).order_by(Equipment.kind, Equipment.name))]


@router.post("/plants/{plant_id}/equipment", status_code=201)
def create_equipment(plant_id: UUID, body: EquipmentIn, request: Request, p: Principal = Depends(require("plants:write")),
                     db: Session = Depends(get_db)) -> dict:
    get_plant(db, plant_id, p)
    data = body.model_dump(exclude={"latitude", "longitude"})
    loc = wkt_point(body.latitude, body.longitude) if body.latitude is not None and body.longitude is not None else None
    e = Equipment(organization_id=p.org_id, plant_id=plant_id, location=loc, **data)
    db.add(e)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="equipment.create", entity_type="equipment", entity_id=e.id,
          details={"name": e.name, "serial": e.serial_number}, ip=client_ip(request))
    db.commit()
    return equipment_out(e, {e.id: (body.latitude, body.longitude)})


@router.patch("/equipment/{equipment_id}")
def update_equipment(equipment_id: UUID, body: dict, request: Request, p: Principal = Depends(require("plants:write")),
                     db: Session = Depends(get_db)) -> dict:
    e = db.get(Equipment, equipment_id)
    if not e or e.organization_id != p.org_id:
        raise HTTPException(404, "Equipment not found")
    merged = EquipmentIn(**{**equipment_out(e), **body})  # re-validate whole record
    for k, v in merged.model_dump(exclude={"latitude", "longitude"}).items():
        setattr(e, k, v)
    if merged.latitude is not None and merged.longitude is not None:
        e.location = wkt_point(merged.latitude, merged.longitude)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="equipment.update", entity_type="equipment", entity_id=e.id,
          details={k: str(v) for k, v in body.items()}, ip=client_ip(request))
    db.commit()
    return equipment_out(e)


# --------------------------------------------------------------------------- energy contracts


class ContractIn(BaseModel):
    kind: Literal["ppa", "feed_in_tariff", "self_consumption", "merchant"]
    counterparty: str | None = None
    price_per_kwh: Decimal = Field(ge=0, le=100)
    currency: str = Field(pattern="^[A-Z]{3}$")
    valid_from: date
    valid_to: date | None = None
    escalation_pct_per_year: Decimal = Field(Decimal(0), ge=-20, le=50)
    curtailment_compensated: bool = False
    export_cap_kw: float | None = Field(None, gt=0)
    notes: str | None = None


def contract_out(c: EnergyContract) -> dict:
    return {"id": str(c.id), "plant_id": str(c.plant_id), "kind": c.kind, "counterparty": c.counterparty,
            "price_per_kwh": float(c.price_per_kwh), "currency": c.currency, "valid_from": c.valid_from.isoformat(),
            "valid_to": c.valid_to.isoformat() if c.valid_to else None, "escalation_pct_per_year": float(c.escalation_pct_per_year),
            "curtailment_compensated": c.curtailment_compensated, "export_cap_kw": c.export_cap_kw, "notes": c.notes}


@router.get("/plants/{plant_id}/contracts")
def list_contracts(plant_id: UUID, p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    get_plant(db, plant_id, p)
    return [contract_out(c) for c in db.scalars(select(EnergyContract).where(EnergyContract.plant_id == plant_id).order_by(EnergyContract.valid_from))]


@router.post("/plants/{plant_id}/contracts", status_code=201)
def create_contract(plant_id: UUID, body: ContractIn, request: Request, p: Principal = Depends(require("plants:write")),
                    db: Session = Depends(get_db)) -> dict:
    get_plant(db, plant_id, p)
    if body.valid_to and body.valid_to < body.valid_from:
        raise HTTPException(422, "valid_to before valid_from")
    c = EnergyContract(organization_id=p.org_id, plant_id=plant_id, **body.model_dump())
    db.add(c)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="contract.create", entity_type="energy_contract", entity_id=c.id,
          details={"kind": c.kind, "price": str(c.price_per_kwh), "currency": c.currency}, ip=client_ip(request))
    db.commit()
    return contract_out(c)


# --------------------------------------------------------------------------- operational events


class EventIn(BaseModel):
    category: Literal["outage", "grid_outage", "curtailment", "planned_maintenance", "inverter_fault", "soiling", "other"]
    start_ts: datetime
    end_ts: datetime
    equipment_id: UUID | None = None
    confirmed: bool = False
    description: str | None = None


def event_out(e: OperationalEvent) -> dict:
    return {"id": str(e.id), "plant_id": str(e.plant_id), "equipment_id": str(e.equipment_id) if e.equipment_id else None,
            "category": e.category, "start_ts": e.start_ts.isoformat(), "end_ts": e.end_ts.isoformat(),
            "duration_hours": round((e.end_ts - e.start_ts).total_seconds() / 3600, 2), "confirmed": e.confirmed,
            "description": e.description, "source": e.source}


@router.get("/plants/{plant_id}/events")
def list_events(plant_id: UUID, p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    get_plant(db, plant_id, p)
    return [event_out(e) for e in db.scalars(select(OperationalEvent).where(OperationalEvent.plant_id == plant_id)
                                             .order_by(OperationalEvent.start_ts.desc()))]


@router.post("/plants/{plant_id}/events", status_code=201)
def create_event(plant_id: UUID, body: EventIn, request: Request, p: Principal = Depends(require("data:import")),
                 db: Session = Depends(get_db)) -> dict:
    get_plant(db, plant_id, p)
    if body.end_ts <= body.start_ts or body.start_ts.tzinfo is None:
        raise HTTPException(422, "end_ts must be after start_ts and timestamps must include a timezone")
    e = OperationalEvent(organization_id=p.org_id, plant_id=plant_id, source="manual", **body.model_dump())
    db.add(e)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="event.create", entity_type="operational_event", entity_id=e.id,
          details={"category": e.category}, ip=client_ip(request))
    db.commit()
    return event_out(e)


# --------------------------------------------------------------------------- exchange rates & sources


class RateIn(BaseModel):
    base: str = Field(pattern="^[A-Z]{3}$")
    quote: str = Field(pattern="^[A-Z]{3}$")
    rate: Decimal = Field(gt=0)
    rate_date: date
    source: str = Field(max_length=200)


@router.get("/exchange-rates")
def list_rates(p: Principal = Depends(require("finance:read")), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": str(r.id), "base": r.base, "quote": r.quote, "rate": float(r.rate), "rate_date": r.rate_date.isoformat(),
             "source": r.source} for r in db.scalars(select(ExchangeRate).order_by(ExchangeRate.rate_date.desc()))]


@router.post("/exchange-rates", status_code=201)
def create_rate(body: RateIn, request: Request, p: Principal = Depends(require("finance:simulate")), db: Session = Depends(get_db)) -> dict:
    r = ExchangeRate(organization_id=p.org_id, **body.model_dump())
    db.add(r)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A rate for this pair and date already exists") from None
    audit(db, org_id=p.org_id, user_id=p.user_id, action="fx.create", entity_type="exchange_rate", entity_id=r.id,
          details=body.model_dump(mode="json"), ip=client_ip(request))
    db.commit()
    return {"id": str(r.id)}


@router.get("/data-sources")
def list_sources(p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": str(s.id), "name": s.name, "kind": s.kind, "provider": s.provider, "url": s.url,
             "license_notes": s.license_notes, "coverage_notes": s.coverage_notes, "model_version": s.model_version,
             "retrieved_at": s.retrieved_at.isoformat() if s.retrieved_at else None, "metadata": s.meta}
            for s in db.scalars(select(DataSource).order_by(DataSource.created_at.desc()))]
