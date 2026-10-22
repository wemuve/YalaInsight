"""Post-repair evaluation: compares estimated vs realised recovery after a work order is completed."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ...models import Alert, Equipment, Plant, WorkOrder
from ..finance import engine as fe
from ..finance.service import tariffs_for
from ..performance import service as perf
from ..performance.engine import DAYLIGHT_POA_WM2


def _pi(db: Session, plant: Plant, start: datetime, end: datetime, inverter: Equipment | None) -> tuple[float | None, float, int]:
    """Performance index (actual ÷ expected) over daylight hours; for an inverter, its share of plant expected."""
    intervals = perf.load_intervals(db, plant, start, end)
    share, inv_rows = 1.0, {}
    if inverter is not None:
        invs = perf.inverters(db, plant.id)
        total = sum(i.dc_capacity_kwp or 0 for i in invs)
        if total and inverter.dc_capacity_kwp:
            share = inverter.dc_capacity_kwp / total
            inv_rows = perf.inverter_hourly(db, plant.id, start, end)
        else:
            inverter = None
    a_sum = e_sum = 0.0
    n = 0
    for iv in intervals:
        if iv.expected_kwh is None or (iv.poa_wm2 or 0) < DAYLIGHT_POA_WM2:
            continue
        actual = inv_rows.get(iv.ts, {}).get(str(inverter.id)) if inverter is not None else iv.actual_kwh
        if actual is None:
            continue
        a_sum += actual
        e_sum += iv.expected_kwh * share
        n += 1
    return (a_sum / e_sum if e_sum > 0 else None), e_sum, n


def evaluate_recovery(db: Session, wo: WorkOrder, window_days: int = 14) -> dict:
    if wo.completed_at is None:
        raise ValueError("work order is not completed")
    plant = db.get(Plant, wo.plant_id)
    eq = db.get(Equipment, wo.equipment_id) if wo.equipment_id else None
    inverter = eq if eq is not None and eq.kind == "inverter" else None
    alert = db.get(Alert, wo.alert_id) if wo.alert_id else None
    ref = alert.window_start if alert else (wo.started_at or wo.created_at)
    if alert:  # measure the impaired state during the alert window if it is long enough
        b_start, b_end = alert.window_start, max(alert.window_end + timedelta(hours=1), alert.window_start + timedelta(days=1))
    else:
        b_start, b_end = ref - timedelta(days=window_days), ref
    _, data_end = perf.data_bounds(db, plant.id)
    a_start = wo.completed_at
    a_end = min(wo.completed_at + timedelta(days=window_days), (data_end or wo.completed_at) + timedelta(hours=1))
    if a_end - a_start < timedelta(days=2):
        return {"status": "insufficient_post_repair_data", "basis": "measured",
                "message": "At least 2 days of production data after completion are needed."}
    pi_before, _, n_b = _pi(db, plant, b_start, b_end, inverter)
    pi_after, exp_after, n_a = _pi(db, plant, a_start, a_end, inverter)
    if pi_before is None or pi_after is None:
        return {"status": "insufficient_data", "basis": "measured"}
    days_after = (a_end - a_start).total_seconds() / 86400
    realised_kwh_year = max(0.0, (pi_after - pi_before) * exp_after * 365 / days_after)
    price = fe.ZERO
    currency = plant.currency
    tariffs = tariffs_for(db, plant)
    if tariffs:
        try:
            t = fe.effective_price([t for t, _ in tariffs], a_start.date())
            price, currency = t.price_on(a_start.date()), t.currency
        except ValueError:
            pass
    realised_value = fe.q(fe.D(realised_kwh_year) * price, fe.MONEY)
    est = wo.est_recovery_kwh_per_year
    return {
        "status": "evaluated", "basis": "measured",
        "scope": f"inverter {inverter.name}" if inverter else "plant",
        "before": {"start": b_start.isoformat(), "end": b_end.isoformat(), "performance_index": round(pi_before, 4), "hours": n_b},
        "after": {"start": a_start.isoformat(), "end": a_end.isoformat(), "performance_index": round(pi_after, 4), "hours": n_a},
        "realised_recovery_kwh_per_year": round(realised_kwh_year, 1),
        "realised_recovery_value_per_year": float(realised_value), "currency": currency,
        "estimated_recovery_kwh_per_year": est,
        "estimated_recovery_value_per_year": float(wo.est_recovery_value_per_year) if wo.est_recovery_value_per_year is not None else None,
        "variance_vs_estimate_pct": round((realised_kwh_year - est) / est * 100, 1) if est else None,
        "actual_cost": float(wo.actual_cost) if wo.actual_cost is not None else None,
        "simple_payback_years": round(float(wo.actual_cost) / float(realised_value), 2)
        if wo.actual_cost and realised_value > 0 else None,
        "caveats": ["Before/after comparison uses the expected-generation model to normalise weather; other changes in "
                    "the same period (cleaning, curtailment, seasonal soiling) can bias the result.",
                    "Annualised from a short post-repair window — re-evaluate after a longer period."],
        "evaluated_at": datetime.now(a_start.tzinfo).isoformat(),
    }
