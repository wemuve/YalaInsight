"""Portfolio KPIs shared by the dashboard, GIS layer and reports."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Alert, Plant, WorkOrder
from ..carbon.router import plant_carbon
from ..finance import engine as fe
from ..finance.service import build_hours, tariffs_for
from ..performance import engine as pe
from ..performance import service as perf

ACTIVE_ALERT = ("open", "acknowledged", "investigating")
CLOSED_WO = ("completed", "verified", "cancelled")


def plant_kpis(db: Session, plant: Plant, s: datetime, e: datetime) -> dict:
    intervals = perf.load_intervals(db, plant, s, e)
    summ = pe.summarize(intervals, plant.dc_capacity_kwp)
    loss = None
    tariffs = tariffs_for(db, plant)
    if tariffs and perf.latest_model_run(db, plant.id):
        try:
            t = fe.effective_price([t for t, _ in tariffs], (e - timedelta(seconds=1)).date())
            contract = next(c for tt, c in tariffs if tt == t)
            hours, _ = build_hours(db, plant, s, e, True, contract.export_cap_kw)
            att = fe.attribute_losses(hours)
            val = fe.value_losses(att, t, (e - timedelta(seconds=1)).date())
            addressable_kwh = sum((att.by_category[c] for c in fe.ADDRESSABLE_CATEGORIES), fe.ZERO)
            loss = {"shortfall_kwh": float(att.total_shortfall_kwh), "gross_value": float(val["gross_value"]),
                    "lost_revenue": float(val["lost_revenue"]), "addressable_kwh": float(addressable_kwh),
                    "addressable_value": float(fe.q(addressable_kwh * val["price_per_kwh"], fe.MONEY)),
                    "currency": t.currency, "basis": "modelled"}
        except ValueError:
            loss = None
    alerts = db.execute(select(func.count(Alert.id), func.coalesce(func.sum(Alert.est_financial_impact), 0),
                               func.max(Alert.severity))
                        .where(Alert.plant_id == plant.id, Alert.status.in_(ACTIVE_ALERT))).one()
    crit = db.scalar(select(func.count(Alert.id)).where(Alert.plant_id == plant.id, Alert.status.in_(ACTIVE_ALERT),
                                                        Alert.severity.in_(("high", "critical"))))
    overdue = db.scalar(select(func.count(WorkOrder.id)).where(WorkOrder.plant_id == plant.id, WorkOrder.status.notin_(CLOSED_WO),
                                                               WorkOrder.due_date < date.today()))
    open_wo = db.scalar(select(func.count(WorkOrder.id)).where(WorkOrder.plant_id == plant.id, WorkOrder.status.notin_(CLOSED_WO)))
    recovery = db.scalar(select(func.coalesce(func.sum(WorkOrder.est_recovery_value_per_year), 0)).where(
        WorkOrder.plant_id == plant.id, WorkOrder.status.notin_(CLOSED_WO)))
    carbon = plant_carbon(db, plant, s, e)
    pr = summ["performance_ratio"]
    status = "no_data" if summ["intervals_compared"] == 0 else (
        "critical" if crit and crit > 0 else "warning" if (pr is not None and pr < plant.expected_pr * 0.9) or alerts[0] else "ok")
    return {
        "plant_id": str(plant.id), "name": plant.name, "code": plant.code, "status": plant.status, "health": status,
        "country_code": plant.country_code, "region": plant.region, "dc_capacity_kwp": plant.dc_capacity_kwp,
        "ac_capacity_kw": plant.ac_capacity_kw, "currency": plant.currency,
        "performance": summ, "loss": loss,
        "alerts": {"active": alerts[0], "high_or_critical": crit, "est_financial_impact": float(alerts[1])},
        "work_orders": {"open": open_wo, "overdue": overdue, "est_recovery_value_per_year": float(recovery)},
        "carbon_tco2e": (carbon["estimate"] or {}).get("estimated_avoided_tco2e"),
    }
