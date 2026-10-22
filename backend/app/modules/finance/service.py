"""Assembles engine inputs from the database and produces traceable loss / recovery analyses."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Alert, EnergyContract, ExchangeRate, OperationalEvent, Plant
from ..performance import service as perf
from . import engine as fe

DEFAULT_RECOVERY_CATEGORIES = ["inverter_failure", "soiling", "excess_degradation"]
# Unreviewed (or confirmed) alerts that may stand in for missing event records during attribution.
ALERT_RULES_USED = ("INVERTER_OFFLINE", "INVERTER_UNDERPERFORMANCE", "PLANT_ZERO_OUTPUT")


@dataclass
class RecoveryParams:
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_RECOVERY_CATEGORIES))
    recovery_rate: float = 0.8
    repair_cost: float = 0.0
    incremental_annual_cost: float = 0.0
    days_to_effect: int = 14
    horizon_years: int = 5
    discount_rate: float = 0.08
    persistent: bool = True  # if False, the loss is assumed already resolved -> no future recovery
    run_rate_days: int = 7    # future loss is projected from the addressable loss rate in this trailing window


@dataclass
class AnalysisRequest:
    start: datetime
    end: datetime
    soiling_pct: float = 0.0
    excess_degradation_pct: float = 0.0
    use_alerts_for_inverter_faults: bool = True
    report_currency: str | None = None
    recovery: RecoveryParams = field(default_factory=RecoveryParams)


class AnalysisError(ValueError):
    pass


def tariffs_for(db: Session, plant: Plant) -> list[tuple[fe.Tariff, EnergyContract]]:
    rows = db.scalars(select(EnergyContract).where(EnergyContract.plant_id == plant.id)).all()
    return [(fe.Tariff(kind=c.kind, price_per_kwh=c.price_per_kwh, currency=c.currency, valid_from=c.valid_from,
                       valid_to=c.valid_to, escalation_pct_per_year=c.escalation_pct_per_year,
                       curtailment_compensated=c.curtailment_compensated), c) for c in rows]


def build_hours(db: Session, plant: Plant, start: datetime, end: datetime, use_alerts: bool,
                export_cap_kw: float | None) -> tuple[list[fe.HourInput], dict]:
    intervals = perf.load_intervals(db, plant, start, end)
    events = db.scalars(select(OperationalEvent).where(
        OperationalEvent.plant_id == plant.id, OperationalEvent.start_ts < end, OperationalEvent.end_ts > start)).all()
    invs = {str(i.id): i for i in perf.inverters(db, plant.id)}
    total_inv_kwp = sum((i.dc_capacity_kwp or 0) for i in invs.values()) or plant.dc_capacity_kwp
    fault_windows: list[tuple[datetime, datetime, str, float]] = []  # start, end, equipment, share of inverter lost
    plant_events: list[tuple[datetime, datetime, str]] = []
    for ev in events:
        if ev.equipment_id and str(ev.equipment_id) in invs and ev.category in {"inverter_fault", "outage"}:
            fault_windows.append((ev.start_ts, ev.end_ts, str(ev.equipment_id), 1.0))
        elif ev.equipment_id is None:
            plant_events.append((ev.start_ts, ev.end_ts, ev.category))
    alert_count = 0
    if use_alerts:
        alerts = db.scalars(select(Alert).where(
            Alert.plant_id == plant.id, Alert.rule_id.in_(ALERT_RULES_USED), Alert.status != "false_positive",
            Alert.window_start < end, Alert.window_end >= start)).all()
        for a in alerts:
            w_end = a.window_end + timedelta(hours=1)
            if a.rule_id == "PLANT_ZERO_OUTPUT":
                plant_events.append((a.window_start, w_end, "outage"))
            elif a.equipment_id and str(a.equipment_id) in invs:
                lost_share = 1.0 if a.rule_id == "INVERTER_OFFLINE" else min(1.0, a.evidence.get("mean_deficit_pct", 0) / 100)
                fault_windows.append((a.window_start, w_end, str(a.equipment_id), lost_share))
            else:
                continue
            alert_count += 1
    hours = []
    cap_kwh = export_cap_kw  # hourly intervals
    for iv in intervals:
        t = iv.ts
        evs = frozenset(c for s, e, c in plant_events if s <= t < e)
        faulted: dict[str, float] = {}
        for s, e, eq, share in fault_windows:
            if s <= t < e:
                faulted[eq] = max(faulted.get(eq, 0.0), share)
        inv_exp = 0.0
        if faulted and iv.expected_kwh:
            inv_exp = iv.expected_kwh * sum((invs[eq].dc_capacity_kwp or 0) * sh for eq, sh in faulted.items()) / total_inv_kwp
        hours.append(fe.HourInput(t, iv.expected_kwh, iv.actual_kwh, evs, inv_exp, cap_kwh))
    meta = {"events_considered": len(events), "inverter_fault_windows": len(fault_windows),
            "alert_windows_used": alert_count,
            "event_hours": {c: fe.total_duration_hours([(max(s, start), min(e, end)) for s, e, cc in plant_events if cc == c]).__float__()
                            for c in {c for _, _, c in plant_events}}}
    return hours, meta


def _fx_rates(db: Session) -> list[fe.FxRate]:
    return [fe.FxRate(r.base, r.quote, r.rate, r.rate_date, r.source) for r in db.scalars(select(ExchangeRate))]


def analyse(db: Session, plant: Plant, req: AnalysisRequest) -> dict:
    if req.end <= req.start:
        raise AnalysisError("end must be after start")
    tariffs = tariffs_for(db, plant)
    if not tariffs:
        raise AnalysisError("No energy contract / tariff configured for this plant. Add one before valuing losses.")
    price_date = (req.end - timedelta(seconds=1)).date()
    try:
        tariff = fe.effective_price([t for t, _ in tariffs], price_date)
    except ValueError as exc:
        raise AnalysisError(str(exc)) from None
    contract = next(c for t, c in tariffs if t == tariff)
    hours, meta = build_hours(db, plant, req.start, req.end, req.use_alerts_for_inverter_faults, contract.export_cap_kw)
    run = perf.latest_model_run(db, plant.id)
    if run is None:
        raise AnalysisError("No expected-generation model run for this plant. Import weather data first.")
    shares = fe.ModelledLossShares(req.soiling_pct, req.excess_degradation_pct)
    period_days = Decimal((req.end - req.start).total_seconds()) / 86400
    rp = req.recovery
    rates = _fx_rates(db) if req.report_currency and req.report_currency != tariff.currency else []

    def money(v: Decimal) -> dict:
        out = {"value": float(v), "currency": tariff.currency}
        if rates:
            conv, r = fe.convert(v, tariff.currency, req.report_currency, rates, price_date)
            out["converted"] = {"value": float(conv), "currency": req.report_currency, "rate": float(r.rate),
                                "rate_date": r.rate_date.isoformat(), "rate_source": r.source} if r else None
        return out

    run_rate_days = min(Decimal(rp.run_rate_days), period_days)
    recent_start = req.end - timedelta(days=float(run_rate_days))
    scenarios = {}
    base_recovery_input = None
    for sc in fe.default_scenarios(run.uncertainty_pct, rp.recovery_rate, rp.days_to_effect):
        att = fe.attribute_losses(hours, shares, sc.expected_scale)
        val = fe.value_losses(att, tariff, price_date)
        addressable = sum((att.by_category[fe.LossCategory(c)] for c in rp.categories), fe.ZERO)
        # Only losses still occurring at the end of the period are projected forward (resolved faults are sunk).
        att_recent = fe.attribute_losses([h for h in hours if h.ts >= recent_start], shares, sc.expected_scale)
        addressable_recent = sum((att_recent.by_category[fe.LossCategory(c)] for c in rp.categories), fe.ZERO)
        annual = fe.annualise(addressable_recent, run_rate_days) if rp.persistent else fe.ZERO
        rin = fe.RecoveryInput(
            annual_addressable_loss_kwh=annual, recovery_rate=sc.recovery_rate,
            energy_price=val["price_per_kwh"] * sc.price_multiplier, repair_cost=fe.D(rp.repair_cost) * sc.repair_cost_multiplier,
            incremental_annual_cost=fe.D(rp.incremental_annual_cost), days_to_effect=sc.days_to_effect,
            horizon_years=rp.horizon_years, discount_rate=fe.D(rp.discount_rate),
            annual_degradation_pct=fe.D(plant.annual_degradation_pct))
        rec = fe.simulate_recovery(rin)
        if sc.name == "base":
            base_recovery_input = rin
        scenarios[sc.name] = {
            "assumptions": {"expected_baseline_multiplier": float(sc.expected_scale), "recovery_rate": float(sc.recovery_rate),
                            "price_multiplier": float(sc.price_multiplier), "repair_cost_multiplier": float(sc.repair_cost_multiplier),
                            "days_to_effect": sc.days_to_effect},
            "energy": {
                "expected_kwh": fe.Figure(att.expected_kwh, "kWh", fe.Basis.MODELLED, "Σ expected (sellable) over compared intervals").to_dict(),
                "actual_kwh": fe.Figure(att.actual_kwh, "kWh", fe.Basis.MEASURED, "Σ metered over compared intervals").to_dict(),
                "shortfall_kwh": fe.Figure(att.total_shortfall_kwh, "kWh", fe.Basis.MODELLED, "Σ attributed shortfall").to_dict(),
                "shortfall_mwh": float(att.total_shortfall_kwh / 1000),
                "overperformance_kwh": float(att.overperformance_kwh),
            },
            "losses": val["categories"],
            "gross_lost_energy_value": money(val["gross_value"]) | {"basis": "modelled",
                                                                    "formula": "Σ attributed lost kWh × energy price"},
            "lost_revenue": money(val["lost_revenue"]) | {"basis": "modelled",
                                                          "formula": "gross value − contractually compensated categories"},
            "recovery": {
                "addressable_categories": rp.categories,
                "addressable_loss_in_period_kwh": float(addressable),
                "addressable_loss_in_run_rate_window_kwh": float(addressable_recent),
                "run_rate_window_days": float(run_rate_days),
                "annualised_addressable_loss_kwh": float(annual),
                "annual_recovered_kwh": float(rec["annual_recovered_kwh"]),
                "annual_recovered_value": money(rec["annual_recovered_value"]),
                "repair_cost": money(fe.q(rin.repair_cost, fe.MONEY)),
                "net_benefit": money(rec["net_benefit"]),
                "npv": money(rec["npv"]),
                "roi": float(rec["roi"]) if rec["roi"] is not None else None,
                "payback_years": float(rec["payback_years"]) if rec["payback_years"] is not None else None,
                "estimated_annual_financial_impact": money(fe.q(annual * val["price_per_kwh"] * sc.price_multiplier, fe.MONEY)),
                "cash_flows": rec["cash_flows"],
                "basis": "simulated",
            },
        }
    base_att = fe.attribute_losses(hours, shares)
    return {
        "engine_version": fe.ENGINE_VERSION,
        "plant": {"id": str(plant.id), "name": plant.name, "dc_capacity_kwp": plant.dc_capacity_kwp},
        "period": {"start": req.start.isoformat(), "end": req.end.isoformat(), "days": float(period_days)},
        "model": {"name": run.model, "version": run.model_version, "run_id": str(run.id),
                  "uncertainty_pct": run.uncertainty_pct, "parameters": run.parameters},
        "data_quality": {"intervals_total": base_att.intervals_total, "intervals_compared": base_att.intervals_used,
                         "intervals_excluded": base_att.intervals_excluded, "coverage": float(base_att.data_coverage),
                         "warning": "Low data coverage — treat results as indicative." if base_att.data_coverage < Decimal("0.9") else None},
        "tariff": {"kind": tariff.kind, "price_per_kwh": float(tariff.price_on(price_date)), "currency": tariff.currency,
                   "price_date": price_date.isoformat(), "curtailment_compensated": tariff.curtailment_compensated,
                   "export_cap_kw": contract.export_cap_kw, "contract_id": str(contract.id),
                   "note": fe.TARIFF_NOTES.get(tariff.kind, "")},
        "attribution_inputs": meta | {"soiling_pct": req.soiling_pct, "excess_degradation_pct": req.excess_degradation_pct},
        "scenarios": scenarios,
        "sensitivity": fe.sensitivity(base_recovery_input) if base_recovery_input else [],
        "assumptions": [
            "Expected generation from model " + f"{run.model} v{run.model_version} with ±{run.uncertainty_pct}% baseline uncertainty (low/high scenarios).",
            "Each kWh of shortfall is attributed once: curtailment > grid outage > planned maintenance > plant downtime > inverter faults > soiling > excess degradation > unexplained.",
            "Intervals missing either metered or expected values are excluded, not counted as zero.",
            f"Historical losses are sunk; recovery values refer to future avoided losses, projected from the addressable loss rate in the final {float(run_rate_days):g} days of the period (resolved faults are not projected; no seasonality adjustment)." if rp.persistent
            else "Loss assumed already resolved: no future recovery is valued.",
            "Lost revenue excludes categories compensated under the energy contract.",
            "Soiling / excess-degradation shares are analyst estimates (simulated) unless backed by inspection records.",
            "Unrecorded outages and inverter faults are taken from anomaly alerts not marked false-positive "
            "(INVERTER_OFFLINE: full inverter share; INVERTER_UNDERPERFORMANCE: measured deficit share; PLANT_ZERO_OUTPUT: downtime)."
            if req.use_alerts_for_inverter_faults else "Only recorded operational events are used for attribution.",
        ],
        "labels": {"measured": "metered data", "modelled": "derived from the expected-generation model",
                   "simulated": "depends on scenario assumptions", "verified": "confirmed by inspection / post-repair measurement"},
    }
