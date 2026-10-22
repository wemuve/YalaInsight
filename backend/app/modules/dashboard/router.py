from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.deps import Principal, require
from ...db import get_db
from ...models import Alert, ExchangeRate, Organization, Plant, WorkOrder
from ..anomaly.router import alert_out
from ..common import resolve_period, scoped
from ..finance import engine as fe
from ..maintenance.router import wo_out
from .service import ACTIVE_ALERT, CLOSED_WO, plant_kpis

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def summary(start: datetime | None = None, end: datetime | None = None, p: Principal = Depends(require("plants:read")),
            db: Session = Depends(get_db)) -> dict:
    plants = db.scalars(scoped(select(Plant), Plant.id, p).order_by(Plant.name)).all()
    s, e = resolve_period(db, [x.id for x in plants] or None, start, end, default_days=30)
    rows = [plant_kpis(db, x, s, e) for x in plants]

    def tot(path: list[str]) -> float:
        v = 0.0
        for r in rows:
            cur = r
            for k in path:
                cur = (cur or {}).get(k) if isinstance(cur, dict) else None
            v += cur or 0
        return v

    org = db.get(Organization, p.org_id)
    target = org.default_currency
    rates = [fe.FxRate(r.base, r.quote, r.rate, r.rate_date, r.source) for r in db.scalars(select(ExchangeRate))]
    fx_used = {}
    for r in rows:  # express each plant's money figures in the organisation currency where a dated rate exists
        if r["loss"] and r["loss"]["currency"] != target:
            try:
                on = (e - timedelta(seconds=1)).date()
                for k in ("gross_value", "lost_revenue", "addressable_value"):
                    r["loss"][k + "_native"] = r["loss"][k]
                    conv, rate = fe.convert(fe.D(r["loss"][k]), r["loss"]["currency"], target, rates, on)
                    r["loss"][k] = float(conv)
                fx_used[r["loss"]["currency"]] = {"rate": float(rate.rate), "base": rate.base, "quote": rate.quote,
                                                  "rate_date": rate.rate_date.isoformat(), "source": rate.source}
                r["loss"]["native_currency"] = r["loss"]["currency"]
                r["loss"]["currency"] = target
            except ValueError:
                pass
    currencies = sorted({r["loss"]["currency"] for r in rows if r["loss"]})
    act, exp = tot(["performance", "actual_kwh_compared"]), tot(["performance", "expected_kwh"])
    ref = sum((r["performance"]["actual_kwh_compared"] / r["performance"]["performance_ratio"])
              for r in rows if r["performance"]["performance_ratio"])
    top_alerts = db.execute(scoped(select(Alert, Plant.name), Alert.plant_id, p).join(Plant, Plant.id == Alert.plant_id)
                            .where(Alert.status.in_(ACTIVE_ALERT)).order_by(Alert.priority_score.desc()).limit(8)).all()
    overdue = db.execute(scoped(select(WorkOrder, Plant.name), WorkOrder.plant_id, p).join(Plant, Plant.id == WorkOrder.plant_id)
                         .where(WorkOrder.status.notin_(CLOSED_WO), WorkOrder.due_date < datetime.now().date())
                         .order_by(WorkOrder.due_date).limit(8)).all()
    return {
        "start": s.isoformat(), "end": e.isoformat(),
        "portfolio": {
            "plant_count": len(plants), "dc_capacity_kwp": sum(x.dc_capacity_kwp for x in plants),
            "ac_capacity_kw": sum(x.ac_capacity_kw for x in plants),
            "actual_kwh": tot(["performance", "actual_kwh"]), "expected_kwh": exp, "actual_kwh_compared": act,
            "variance_pct": (act - exp) / exp * 100 if exp else None,
            "performance_ratio": act / ref if ref else None,
            "availability": (sum((r["performance"]["availability"] or 0) * r["dc_capacity_kwp"] for r in rows) /
                             sum(r["dc_capacity_kwp"] for r in rows)) if rows else None,
            "loss_currency": currencies[0] if len(currencies) == 1 else None,
            "mixed_currencies": currencies if len(currencies) > 1 else None,
            "gross_lost_value": tot(["loss", "gross_value"]) if len(currencies) == 1 else None,
            "lost_revenue": tot(["loss", "lost_revenue"]) if len(currencies) == 1 else None,
            "addressable_value": tot(["loss", "addressable_value"]) if len(currencies) == 1 else None,
            "shortfall_kwh": tot(["loss", "shortfall_kwh"]),
            "active_alerts": int(tot(["alerts", "active"])), "high_alerts": int(tot(["alerts", "high_or_critical"])),
            "overdue_work_orders": int(tot(["work_orders", "overdue"])),
            "open_work_orders": int(tot(["work_orders", "open"])),
            "recovery_pipeline_value_per_year": tot(["work_orders", "est_recovery_value_per_year"]),
            "carbon_tco2e_estimated": tot(["carbon_tco2e"]),
            "fx_rates_used": fx_used,
        },
        "plants": rows,
        "top_alerts": [alert_out(a, pn) for a, pn in top_alerts],
        "overdue_work_orders": [wo_out(w, pn) for w, pn in overdue],
        "basis": {"actual_kwh": "measured", "expected_kwh": "modelled", "losses": "modelled",
                  "carbon": "estimated (not verified)", "recovery_pipeline": "simulated (work-order estimates)"},
    }
