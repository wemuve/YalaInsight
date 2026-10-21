from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.deps import Principal, require, require_module
from ...db import get_db
from ..common import get_plant, resolve_period
from . import engine as fe
from .service import AnalysisError, AnalysisRequest, RecoveryParams, analyse

router = APIRouter(tags=["finance"], dependencies=[Depends(require_module("finance"))])

CATEGORIES = [c.value for c in fe.LossCategory]


class RecoveryIn(BaseModel):
    categories: list[str] = Field(default_factory=lambda: ["inverter_failure", "soiling", "excess_degradation"])
    recovery_rate: float = Field(0.8, ge=0, le=1)
    repair_cost: float = Field(0, ge=0)
    incremental_annual_cost: float = Field(0, ge=0)
    days_to_effect: int = Field(14, ge=0, le=365)
    horizon_years: int = Field(5, ge=1, le=30)
    discount_rate: float = Field(0.08, ge=0, le=1)
    persistent: bool = True
    run_rate_days: int = Field(7, ge=1, le=365)


class AnalysisIn(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    soiling_pct: float = Field(0, ge=0, le=50)
    excess_degradation_pct: float = Field(0, ge=0, le=50)
    use_alerts_for_inverter_faults: bool = True
    report_currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    recovery: RecoveryIn = Field(default_factory=RecoveryIn)


@router.post("/plants/{plant_id}/financial-analysis")
def financial_analysis(plant_id: UUID, body: AnalysisIn, p: Principal = Depends(require("finance:read")),
                       db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    bad = [c for c in body.recovery.categories if c not in CATEGORIES]
    if bad:
        raise HTTPException(422, f"unknown loss categories {bad}; valid: {CATEGORIES}")
    s, e = resolve_period(db, [plant.id], body.start, body.end, default_days=30)
    req = AnalysisRequest(start=s, end=e, soiling_pct=body.soiling_pct, excess_degradation_pct=body.excess_degradation_pct,
                          use_alerts_for_inverter_faults=body.use_alerts_for_inverter_faults, report_currency=body.report_currency,
                          recovery=RecoveryParams(**body.recovery.model_dump()))
    try:
        return analyse(db, plant, req)
    except AnalysisError as exc:
        raise HTTPException(422, str(exc)) from None
    except ValueError as exc:  # e.g. missing FX rate
        raise HTTPException(422, str(exc)) from None


class SimulationIn(BaseModel):
    annual_addressable_loss_kwh: Decimal = Field(ge=0)
    recovery_rate: Decimal = Field(ge=0, le=1)
    energy_price: Decimal = Field(ge=0)
    repair_cost: Decimal = Field(ge=0)
    incremental_annual_cost: Decimal = Field(Decimal(0), ge=0)
    days_to_effect: int = Field(0, ge=0, le=365)
    horizon_years: int = Field(5, ge=1, le=30)
    discount_rate: Decimal = Field(Decimal("0.08"), ge=0, le=1)
    annual_degradation_pct: Decimal = Field(Decimal("0.5"), ge=0, le=5)
    price_escalation_pct: Decimal = Field(Decimal(0), ge=-20, le=50)
    sensitivity_variation: Decimal = Field(Decimal("0.2"), gt=0, lt=1)
    currency: str = Field("USD", pattern="^[A-Z]{3}$")


@router.post("/finance/recovery-simulation")
def recovery_simulation(body: SimulationIn, p: Principal = Depends(require("finance:simulate"))) -> dict:
    """Stand-alone what-if: value of fixing a loss of known annual size. All outputs are 'simulated'."""
    data = body.model_dump(exclude={"sensitivity_variation", "currency"})
    inp = fe.RecoveryInput(**data)
    res = fe.simulate_recovery(inp)
    return {"engine_version": fe.ENGINE_VERSION, "basis": "simulated", "currency": body.currency,
            "inputs": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in data.items()},
            "result": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in res.items()},
            "sensitivity": fe.sensitivity(inp, body.sensitivity_variation),
            "formulae": {
                "annual_recovered_kwh": "annual_addressable_loss_kwh × recovery_rate",
                "year_value": "recovered_kwh × (1 − degradation)^(y−1) × price × (1 + escalation)^(y−1); year 1 pro-rated by days_to_effect",
                "net_benefit": "Σ (year_value − incremental_annual_cost) − repair_cost",
                "npv": "−repair_cost + Σ net_cash_flow_y ÷ (1 + discount_rate)^y",
                "roi": "net_benefit ÷ (repair_cost + incremental_annual_cost × horizon)",
                "payback_years": "time until cumulative net cash flow ≥ 0 (linear within year)",
            }}
