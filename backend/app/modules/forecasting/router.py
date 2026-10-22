from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...core.deps import Principal, require, require_module
from ...db import get_db
from ..common import get_plant
from ..performance import service as perf
from . import engine

router = APIRouter(tags=["forecasting"], dependencies=[Depends(require_module("forecasting"))])


def _history(db: Session, plant, end: datetime, days: int) -> list[engine.Obs]:  # noqa: ANN001
    return [engine.Obs(iv.ts, iv.actual_kwh) for iv in perf.load_intervals(db, plant, end - timedelta(days=days), end)]


@router.get("/plants/{plant_id}/forecast")
def forecast(plant_id: UUID, start: datetime | None = None, hours: int = Query(24, ge=1, le=168),
             lookback_days: int = Query(30, ge=7, le=120), p: Principal = Depends(require("plants:read")),
             db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    if start is None:
        _, last = perf.data_bounds(db, plant.id)
        base = last or datetime.now(UTC)
        start = (base + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out = engine.climatology_forecast(_history(db, plant, start, lookback_days), start, hours, lookback_days)
    out["basis"] = "modelled"
    out["note"] = ("Climatological baseline (no weather forecast input). Day-ahead accuracy improves substantially with a "
                   "licensed irradiance forecast fed through the expected-generation model.")
    return out


@router.get("/plants/{plant_id}/forecast/backtest")
def forecast_backtest(plant_id: UUID, days: int = Query(14, ge=1, le=60), p: Principal = Depends(require("plants:read")),
                      db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    _, last = perf.data_bounds(db, plant.id)
    if last is None:
        return {"days": 0, "warnings": ["No production data"]}
    end = (last + timedelta(hours=1))
    return engine.backtest(_history(db, plant, end, days + 31), days, 30)
