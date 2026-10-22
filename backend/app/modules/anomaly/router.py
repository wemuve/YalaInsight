from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.audit import audit
from ...core.deps import Principal, client_ip, require, require_module
from ...db import get_db
from ...models import Alert, Equipment, Plant, WorkOrder
from ..common import get_plant, in_scope, resolve_period, scoped
from ..integrations.webhooks import deliver
from . import service

router = APIRouter(tags=["anomalies"], dependencies=[Depends(require_module("anomaly"))])


def alert_out(a: Alert, plant_name: str | None = None, equipment_name: str | None = None) -> dict:
    return {"id": str(a.id), "plant_id": str(a.plant_id), "plant_name": plant_name,
            "equipment_id": str(a.equipment_id) if a.equipment_id else None, "equipment_name": equipment_name,
            "rule_id": a.rule_id, "rule_version": a.rule_version, "category": a.category, "severity": a.severity,
            "confidence": a.confidence, "status": a.status, "title": a.title, "explanation": a.explanation,
            "evidence": a.evidence, "recommended_actions": a.recommended_actions, "window_start": a.window_start.isoformat(),
            "window_end": a.window_end.isoformat(), "est_energy_loss_kwh": a.est_energy_loss_kwh,
            "est_financial_impact": float(a.est_financial_impact), "currency": a.currency, "urgency_score": a.urgency_score,
            "priority_score": a.priority_score, "finding_notes": a.finding_notes,
            "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None, "created_at": a.created_at.isoformat(),
            "basis": "verified" if a.status == "confirmed" else "modelled",
            "fault_confirmed": a.status == "confirmed"}


class DetectIn(BaseModel):
    start: datetime | None = None
    end: datetime | None = None


@router.post("/plants/{plant_id}/anomaly-detection")
def run_detection(plant_id: UUID, body: DetectIn, request: Request, background: BackgroundTasks,
                  p: Principal = Depends(require("alerts:run")), db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    s, e = resolve_period(db, [plant.id], body.start, body.end, default_days=90)
    res = service.detect(db, plant, s, e)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="anomaly.run", entity_type="plant", entity_id=plant.id,
          details={k: v for k, v in res.items() if k != "delivery_ids"}, ip=client_ip(request))
    db.commit()
    background.add_task(deliver, res.pop("delivery_ids"))
    return {"plant_id": str(plant.id), "start": s.isoformat(), "end": e.isoformat(), **res}


@router.get("/alerts")
def list_alerts(plant_id: UUID | None = None, status: str | None = None, severity: str | None = None,
                limit: int = 100, p: Principal = Depends(require("alerts:read")), db: Session = Depends(get_db)) -> list[dict]:
    q = select(Alert, Plant.name, Equipment.name).join(Plant, Plant.id == Alert.plant_id).outerjoin(
        Equipment, Equipment.id == Alert.equipment_id)
    q = scoped(q, Alert.plant_id, p)
    if plant_id:
        q = q.where(Alert.plant_id == plant_id)
    if status == "active":
        q = q.where(Alert.status.in_(["open", "acknowledged", "investigating"]))
    elif status:
        q = q.where(Alert.status == status)
    if severity:
        q = q.where(Alert.severity == severity)
    q = q.order_by(Alert.priority_score.desc(), Alert.window_end.desc()).limit(min(limit, 500))
    return [alert_out(a, pn, en) for a, pn, en in db.execute(q)]


@router.get("/alerts/stats")
def alert_stats(p: Principal = Depends(require("alerts:read")), db: Session = Depends(get_db)) -> dict:
    return {"by_rule": service.review_stats(db, p.plant_scope),
            "note": "precision = confirmed ÷ (confirmed + false positive) among reviewed alerts; used to validate rules in pilots."}


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: UUID, p: Principal = Depends(require("alerts:read")), db: Session = Depends(get_db)) -> dict:
    a = db.get(Alert, alert_id)
    if not a or a.organization_id != p.org_id or not in_scope(p, a.plant_id):
        raise HTTPException(404, "Alert not found")
    plant = db.get(Plant, a.plant_id)
    eq = db.get(Equipment, a.equipment_id) if a.equipment_id else None
    out = alert_out(a, plant.name, eq.name if eq else None)
    out["work_orders"] = [{"id": str(w.id), "title": w.title, "status": w.status}
                          for w in db.scalars(select(WorkOrder).where(WorkOrder.alert_id == a.id))]
    return out


class ReviewIn(BaseModel):
    status: Literal["acknowledged", "investigating", "confirmed", "false_positive", "resolved"]
    finding_notes: str | None = Field(None, max_length=5000)


@router.post("/alerts/{alert_id}/review")
def review_alert(alert_id: UUID, body: ReviewIn, request: Request, p: Principal = Depends(require("alerts:review")),
                 db: Session = Depends(get_db)) -> dict:
    a = db.get(Alert, alert_id)
    if not a or a.organization_id != p.org_id or not in_scope(p, a.plant_id):
        raise HTTPException(404, "Alert not found")
    if body.status in ("confirmed", "false_positive") and not body.finding_notes:
        raise HTTPException(422, "finding_notes are required to confirm or reject an alert (record the inspection evidence)")
    a.status = body.status
    if body.finding_notes:
        a.finding_notes = body.finding_notes
    a.reviewed_by, a.reviewed_at = p.user_id, datetime.now(UTC)
    audit(db, org_id=p.org_id, user_id=p.user_id, action="alert.review", entity_type="alert", entity_id=a.id,
          details={"status": body.status}, ip=client_ip(request))
    db.commit()
    return alert_out(a)
