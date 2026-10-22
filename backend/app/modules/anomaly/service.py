"""Runs anomaly rules for a plant and upserts alerts (idempotent via dedup keys)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Alert, OperationalEvent, Plant
from ..finance import engine as fe
from ..finance.service import tariffs_for
from ..integrations.webhooks import queue_event
from ..performance import service as perf
from . import rules

EXCLUDING_EVENTS = {"curtailment", "grid_outage", "planned_maintenance"}


def detect(db: Session, plant: Plant, start: datetime, end: datetime, cfg: rules.RuleConfig = rules.RuleConfig()) -> dict:
    intervals = perf.load_intervals(db, plant, start, end)
    inv_rows = perf.inverter_hourly(db, plant.id, start, end)
    evs = db.scalars(select(OperationalEvent).where(
        OperationalEvent.plant_id == plant.id, OperationalEvent.equipment_id.is_(None),
        OperationalEvent.category.in_(EXCLUDING_EVENTS), OperationalEvent.start_ts < end, OperationalEvent.end_ts > start)).all()
    windows = [(e.start_ts, e.end_ts) for e in evs]
    invs = [rules.InverterInfo(str(i.id), i.name, i.dc_capacity_kwp or i.rated_power_kw or 0) for i in perf.inverters(db, plant.id)]
    invs = [i for i in invs if i.dc_kwp > 0]
    hours = [rules.HourPoint(iv.ts, iv.actual_kwh, iv.expected_kwh, iv.poa_wm2,
                             excluded=any(s <= iv.ts < e for s, e in windows), inverters=inv_rows.get(iv.ts, {}))
             for iv in intervals]
    findings = rules.run_all(hours, invs, cfg)
    price, currency = Decimal(0), plant.currency
    tariffs = tariffs_for(db, plant)
    if tariffs:
        try:
            t = fe.effective_price([t for t, _ in tariffs], (end - timedelta(seconds=1)).date())
            price, currency = t.price_on((end - timedelta(seconds=1)).date()), t.currency
        except ValueError:
            pass
    data_end = max((iv.ts for iv in intervals if iv.actual_kwh is not None), default=end)
    created, updated = [], 0
    for f in findings:
        impact = fe.q(fe.D(f.energy_loss_kwh) * price, fe.MONEY)
        urgency, score = rules.priority_score(f.severity, float(impact), f.window_end, data_end)
        existing = db.scalar(select(Alert).where(Alert.organization_id == plant.organization_id, Alert.dedup_key == f.dedup_key))
        fields = dict(rule_version=f.rule_version, category=f.category, severity=f.severity, confidence=f.confidence,
                      title=f.title, explanation=f.explanation, evidence=f.evidence, recommended_actions=f.recommended_actions,
                      window_end=f.window_end, est_energy_loss_kwh=round(f.energy_loss_kwh, 3), est_financial_impact=impact,
                      currency=currency, urgency_score=urgency, priority_score=score)
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            updated += 1
            continue
        a = Alert(organization_id=plant.organization_id, plant_id=plant.id,
                  equipment_id=UUID(f.equipment_id) if f.equipment_id else None, rule_id=f.rule_id,
                  window_start=f.window_start, dedup_key=f.dedup_key, status="open", **fields)
        db.add(a)
        db.flush()
        created.append(a)
    delivery_ids = []
    for a in created:
        delivery_ids += queue_event(db, plant.organization_id, "alert.created", alert_summary(a))
    return {"findings": len(findings), "created": len(created), "updated": updated, "delivery_ids": delivery_ids}


def alert_summary(a: Alert) -> dict:
    return {"id": str(a.id), "plant_id": str(a.plant_id), "equipment_id": str(a.equipment_id) if a.equipment_id else None,
            "rule_id": a.rule_id, "severity": a.severity, "title": a.title, "status": a.status,
            "est_energy_loss_kwh": a.est_energy_loss_kwh, "est_financial_impact": float(a.est_financial_impact),
            "currency": a.currency, "window_start": a.window_start.isoformat(), "window_end": a.window_end.isoformat()}


def review_stats(db: Session, plant_scope: list | None = None) -> dict:
    """Stage-2 validation metric: share of reviewed alerts confirmed as real findings, per rule."""
    out: dict[str, dict] = {}
    q = select(Alert) if plant_scope is None else select(Alert).where(Alert.plant_id.in_(plant_scope))
    for a in db.scalars(q):
        s = out.setdefault(a.rule_id, {"total": 0, "confirmed": 0, "false_positive": 0, "open": 0})
        s["total"] += 1
        if a.status in ("confirmed", "resolved"):
            s["confirmed"] += 1
        elif a.status == "false_positive":
            s["false_positive"] += 1
        else:
            s["open"] += 1
    for s in out.values():
        reviewed = s["confirmed"] + s["false_positive"]
        s["precision"] = round(s["confirmed"] / reviewed, 3) if reviewed else None
    return out
