from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.audit import audit
from ...core.deps import Principal, client_ip, require, require_module
from ...db import get_db
from ...models import ModelRun
from ..common import get_plant, resolve_period
from . import engine, service

router = APIRouter(tags=["performance"], dependencies=[Depends(require_module("performance"))])


@router.get("/plants/{plant_id}/performance")
def performance(plant_id: UUID, granularity: Literal["hour", "day", "month", "year"] = "day",
                start: datetime | None = None, end: datetime | None = None,
                p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    s, e = resolve_period(db, [plant.id], start, end, default_days=7 if granularity == "hour" else 30)
    intervals = service.load_intervals(db, plant, s, e)
    run = service.latest_model_run(db, plant.id)
    summary = engine.summarize(intervals, plant.dc_capacity_kwp)
    flags = []
    if summary["data_coverage"] is not None and summary["data_coverage"] < 0.9:
        flags.append(f"Only {summary['data_coverage']:.0%} of intervals have both metered and expected values.")
    if summary["performance_ratio"] is not None and summary["performance_ratio"] < plant.expected_pr * 0.9:
        flags.append(f"Performance ratio {summary['performance_ratio']:.1%} is >10% below the plant's expected PR "
                     f"of {plant.expected_pr:.0%}.")
    return {
        "plant_id": str(plant.id), "granularity": granularity, "start": s.isoformat(), "end": e.isoformat(),
        "summary": summary, "series": engine.aggregate(intervals, granularity, plant.dc_capacity_kwp), "flags": flags,
        "model": {"name": run.model, "version": run.model_version, "run_id": str(run.id), "uncertainty_pct": run.uncertainty_pct}
        if run else None,
        "basis": {"actual": "measured", "expected": "modelled"},
        "definitions": {
            "performance_ratio": "Σ actual kWh ÷ Σ (kWp × POA kWh/m²) over intervals with both values (IEC 61724-1, not temperature-corrected)",
            "performance_index": "Σ actual ÷ Σ modelled expected (temperature, losses and degradation adjusted)",
            "availability": "share of daylight hours (POA ≥ 50 W/m²) with output > 1% of expected (time-based)",
            "specific_yield": "Σ actual kWh ÷ installed kWp",
        },
    }


@router.post("/plants/{plant_id}/model-runs", status_code=201)
def run_model(plant_id: UUID, request: Request, start: datetime | None = None, end: datetime | None = None,
              p: Principal = Depends(require("data:import")), db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    run = service.recompute_expected(db, plant, start, end)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="model.run", entity_type="model_run", entity_id=run.id,
          details={"hours": run.hours_modelled}, ip=client_ip(request))
    db.commit()
    return model_run_out(run)


def model_run_out(r: ModelRun) -> dict:
    return {"id": str(r.id), "model": r.model, "model_version": r.model_version, "parameters": r.parameters,
            "uncertainty_pct": r.uncertainty_pct, "hours_modelled": r.hours_modelled,
            "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None, "created_at": r.created_at.isoformat()}


@router.get("/plants/{plant_id}/model-runs")
def list_runs(plant_id: UUID, p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    get_plant(db, plant_id, p)
    return [model_run_out(r) for r in db.scalars(select(ModelRun).where(ModelRun.plant_id == plant_id)
                                                .order_by(ModelRun.created_at.desc()).limit(50))]
