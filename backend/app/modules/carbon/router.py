from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from ...core.audit import audit
from ...core.deps import Principal, client_ip, require, require_module
from ...db import get_db
from ...models import EmissionFactor, Plant, VerifiedReduction
from ..common import get_plant, resolve_period
from .engine import FactorRef, avoided_emissions

router = APIRouter(tags=["carbon"], dependencies=[Depends(require_module("carbon"))])

GEN_SQL = text("""SELECT coalesce(sum(energy_kwh), 0) FROM energy_readings
                  WHERE plant_id = :p AND ts >= :s AND ts < :e
                    AND (equipment_id IS NULL OR NOT EXISTS (SELECT 1 FROM energy_readings m WHERE m.plant_id = :p AND m.equipment_id IS NULL))""")


def factor_for(db: Session, plant: Plant) -> EmissionFactor | None:
    if plant.emission_factor_id:
        return db.get(EmissionFactor, plant.emission_factor_id)
    return db.scalar(select(EmissionFactor).where(EmissionFactor.country_code == plant.country_code)
                     .order_by(EmissionFactor.organization_id.is_(None), EmissionFactor.year.desc().nulls_last()).limit(1))


def plant_carbon(db: Session, plant: Plant, s: datetime, e: datetime) -> dict:
    f = factor_for(db, plant)
    gen = db.execute(GEN_SQL, {"p": plant.id, "s": s, "e": e}).scalar()
    verified = db.scalars(select(VerifiedReduction).where(VerifiedReduction.plant_id == plant.id,
                                                          VerifiedReduction.period_end >= s.date(),
                                                          VerifiedReduction.period_start <= e.date())).all()
    est = None
    if f:
        est = avoided_emissions(gen, FactorRef(f.name, f.kg_co2e_per_kwh, f.method, f.source, f.year, f.is_sample),
                                s.date(), (e - timedelta(seconds=1)).date())
        est["estimated_avoided_tco2e"] = float(est["estimated_avoided_tco2e"])
        est["generation_kwh"] = float(est["generation_kwh"])
    return {"plant_id": str(plant.id), "plant_name": plant.name, "estimate": est,
            "warning": None if f else "No emission factor configured for this plant or its country.",
            "verified_reductions": [vr_out(v) for v in verified],
            "note": "Estimated avoided emissions are not verified emission reductions and are not carbon credits."}


@router.get("/plants/{plant_id}/carbon")
def carbon(plant_id: UUID, start: datetime | None = None, end: datetime | None = None,
           p: Principal = Depends(require("carbon:read")), db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    s, e = resolve_period(db, [plant.id], start, end, default_days=365)
    return {"start": s.isoformat(), "end": e.isoformat(), **plant_carbon(db, plant, s, e)}


@router.get("/carbon/portfolio")
def carbon_portfolio(start: datetime | None = None, end: datetime | None = None,
                     p: Principal = Depends(require("carbon:read")), db: Session = Depends(get_db)) -> dict:
    plants = db.scalars(select(Plant)).all()
    s, e = resolve_period(db, [x.id for x in plants], start, end, default_days=365)
    rows = [plant_carbon(db, x, s, e) for x in plants]
    total = sum((r["estimate"] or {}).get("estimated_avoided_tco2e", 0) for r in rows)
    verified = sum(v["tco2e"] for r in rows for v in r["verified_reductions"])
    return {"start": s.isoformat(), "end": e.isoformat(), "estimated_avoided_tco2e": round(total, 3),
            "verified_tco2e": round(verified, 3), "plants": rows,
            "note": "Estimated and verified figures are reported separately and must not be summed."}


class FactorIn(BaseModel):
    name: str = Field(max_length=200)
    country_code: str | None = Field(None, pattern="^[A-Z]{2}$")
    grid_region: str | None = None
    kg_co2e_per_kwh: Decimal = Field(ge=0, le=2)
    method: str = Field(max_length=80)
    source: str
    source_url: str | None = None
    year: int | None = Field(None, ge=1990, le=2100)


def factor_out(f: EmissionFactor) -> dict:
    return {"id": str(f.id), "name": f.name, "country_code": f.country_code, "grid_region": f.grid_region,
            "kg_co2e_per_kwh": float(f.kg_co2e_per_kwh), "method": f.method, "source": f.source, "source_url": f.source_url,
            "year": f.year, "is_sample": f.is_sample, "shared": f.organization_id is None}


@router.get("/emission-factors")
def list_factors(p: Principal = Depends(require("carbon:read")), db: Session = Depends(get_db)) -> list[dict]:
    return [factor_out(f) for f in db.scalars(select(EmissionFactor).where(
        or_(EmissionFactor.organization_id.is_(None), EmissionFactor.organization_id == p.org_id)).order_by(EmissionFactor.country_code))]


@router.post("/emission-factors", status_code=201)
def create_factor(body: FactorIn, request: Request, p: Principal = Depends(require("carbon:write")), db: Session = Depends(get_db)) -> dict:
    f = EmissionFactor(organization_id=p.org_id, is_sample=False, **body.model_dump())
    db.add(f)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="emission_factor.create", entity_type="emission_factor",
          entity_id=f.id, details=body.model_dump(mode="json"), ip=client_ip(request))
    db.commit()
    return factor_out(f)


class VerifiedIn(BaseModel):
    plant_id: UUID
    period_start: date
    period_end: date
    tco2e: Decimal = Field(ge=0)
    standard: str
    verifier: str
    certificate_ref: str | None = None
    credits_issued: Decimal | None = Field(None, ge=0)
    notes: str | None = None


def vr_out(v: VerifiedReduction) -> dict:
    return {"id": str(v.id), "plant_id": str(v.plant_id), "period_start": v.period_start.isoformat(),
            "period_end": v.period_end.isoformat(), "tco2e": float(v.tco2e), "standard": v.standard, "verifier": v.verifier,
            "certificate_ref": v.certificate_ref, "credits_issued": float(v.credits_issued) if v.credits_issued is not None else None,
            "notes": v.notes, "basis": "verified"}


@router.post("/verified-reductions", status_code=201)
def create_verified(body: VerifiedIn, request: Request, p: Principal = Depends(require("carbon:write")), db: Session = Depends(get_db)) -> dict:
    get_plant(db, body.plant_id, p)
    if body.period_end < body.period_start:
        raise HTTPException(422, "period_end before period_start")
    v = VerifiedReduction(organization_id=p.org_id, **body.model_dump())
    db.add(v)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="verified_reduction.create", entity_type="verified_reduction",
          entity_id=v.id, details={"tco2e": str(v.tco2e), "standard": v.standard}, ip=client_ip(request))
    db.commit()
    return vr_out(v)
