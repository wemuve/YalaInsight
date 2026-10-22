"""Work orders, their history, and asset service records (inspections, commissioning, handovers...)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.audit import audit
from ...core.deps import Principal, client_ip, require, require_module
from ...db import get_db
from ...models import Alert, AssetRecord, Equipment, Plant, User, WorkOrder, WorkOrderEvent
from ..common import get_plant, in_scope, scoped
from ..integrations.webhooks import deliver, queue_event
from .service import evaluate_recovery

router = APIRouter(tags=["maintenance"], dependencies=[Depends(require_module("maintenance"))])

STATUSES = ["open", "assigned", "in_progress", "on_hold", "completed", "verified", "cancelled"]
TRANSITIONS = {
    "open": {"assigned", "in_progress", "cancelled", "on_hold"},
    "assigned": {"in_progress", "on_hold", "cancelled", "open"},
    "in_progress": {"on_hold", "completed", "cancelled"},
    "on_hold": {"in_progress", "assigned", "cancelled"},
    "completed": {"verified", "in_progress"},
    "verified": set(),
    "cancelled": {"open"},
}
CLOSED = {"completed", "verified", "cancelled"}


def wo_out(w: WorkOrder, plant_name: str | None = None, assignee: str | None = None) -> dict:
    today = date.today()
    return {"id": str(w.id), "plant_id": str(w.plant_id), "plant_name": plant_name,
            "equipment_id": str(w.equipment_id) if w.equipment_id else None, "alert_id": str(w.alert_id) if w.alert_id else None,
            "title": w.title, "description": w.description, "kind": w.kind, "priority": w.priority, "status": w.status,
            "assignee_id": str(w.assignee_id) if w.assignee_id else None, "assignee_name": assignee, "contractor": w.contractor,
            "due_date": w.due_date.isoformat() if w.due_date else None,
            "overdue": bool(w.due_date and w.due_date < today and w.status not in CLOSED),
            "started_at": w.started_at.isoformat() if w.started_at else None,
            "completed_at": w.completed_at.isoformat() if w.completed_at else None, "currency": w.currency,
            "estimated_cost": float(w.estimated_cost) if w.estimated_cost is not None else None,
            "actual_cost": float(w.actual_cost) if w.actual_cost is not None else None,
            "est_recovery_kwh_per_year": w.est_recovery_kwh_per_year,
            "est_recovery_value_per_year": float(w.est_recovery_value_per_year) if w.est_recovery_value_per_year is not None else None,
            "realised": w.realised, "created_at": w.created_at.isoformat() if w.created_at else None}


class WorkOrderIn(BaseModel):
    plant_id: UUID
    equipment_id: UUID | None = None
    alert_id: UUID | None = None
    title: str = Field(max_length=300)
    description: str | None = None
    kind: Literal["corrective", "preventive", "inspection", "commissioning"] = "corrective"
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    assignee_id: UUID | None = None
    contractor: str | None = None
    due_date: date | None = None
    estimated_cost: Decimal | None = Field(None, ge=0)
    currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    est_recovery_kwh_per_year: float | None = Field(None, ge=0)
    est_recovery_value_per_year: Decimal | None = Field(None, ge=0)


class WorkOrderPatch(BaseModel):
    status: str | None = None
    assignee_id: UUID | None = None
    contractor: str | None = None
    priority: Literal["low", "medium", "high", "critical"] | None = None
    due_date: date | None = None
    actual_cost: Decimal | None = Field(None, ge=0)
    note: str | None = Field(None, max_length=5000)


@router.get("/work-orders")
def list_work_orders(plant_id: UUID | None = None, status: str | None = None, overdue: bool | None = None,
                     p: Principal = Depends(require("workorders:read")), db: Session = Depends(get_db)) -> list[dict]:
    q = select(WorkOrder, Plant.name, User.full_name).join(Plant, Plant.id == WorkOrder.plant_id).outerjoin(
        User, User.id == WorkOrder.assignee_id)
    q = scoped(q, WorkOrder.plant_id, p)
    if plant_id:
        q = q.where(WorkOrder.plant_id == plant_id)
    if status == "active":
        q = q.where(WorkOrder.status.notin_(CLOSED))
    elif status:
        q = q.where(WorkOrder.status == status)
    rows = [wo_out(w, pn, an) for w, pn, an in db.execute(q.order_by(WorkOrder.due_date.asc().nulls_last(), WorkOrder.created_at.desc()))]
    if overdue is not None:
        rows = [r for r in rows if r["overdue"] == overdue]
    return rows


def _validate_refs(db: Session, p: Principal, plant_id: UUID, equipment_id: UUID | None, alert_id: UUID | None,
                   assignee_id: UUID | None) -> None:
    get_plant(db, plant_id, p)
    if equipment_id:
        e = db.get(Equipment, equipment_id)
        if not e or e.plant_id != plant_id:
            raise HTTPException(422, "equipment does not belong to this plant")
    if alert_id:
        a = db.get(Alert, alert_id)
        if not a or a.plant_id != plant_id:
            raise HTTPException(422, "alert does not belong to this plant")
    if assignee_id:
        u = db.get(User, assignee_id)
        if not u or u.organization_id != p.org_id:
            raise HTTPException(422, "unknown assignee")


@router.post("/work-orders", status_code=201)
def create_work_order(body: WorkOrderIn, request: Request, background: BackgroundTasks,
                      p: Principal = Depends(require("workorders:write")), db: Session = Depends(get_db)) -> dict:
    _validate_refs(db, p, body.plant_id, body.equipment_id, body.alert_id, body.assignee_id)
    plant = db.get(Plant, body.plant_id)
    data = body.model_dump()
    data["currency"] = data["currency"] or plant.currency
    w = WorkOrder(organization_id=p.org_id, created_by=p.user_id, status="assigned" if body.assignee_id else "open", **data)
    db.add(w)
    db.flush()
    db.add(WorkOrderEvent(organization_id=p.org_id, work_order_id=w.id, kind="created", message=w.title, user_id=p.user_id,
                          data={"alert_id": str(body.alert_id) if body.alert_id else None}))
    if body.alert_id:
        a = db.get(Alert, body.alert_id)
        if a.status == "open":
            a.status = "investigating"
    audit(db, org_id=p.org_id, user_id=p.user_id, action="workorder.create", entity_type="work_order", entity_id=w.id,
          details={"title": w.title}, ip=client_ip(request))
    ids = queue_event(db, p.org_id, "work_order.created", wo_out(w, plant.name))
    db.commit()
    background.add_task(deliver, ids)
    return wo_out(w, plant.name)


@router.get("/work-orders/{wo_id}")
def get_work_order(wo_id: UUID, p: Principal = Depends(require("workorders:read")), db: Session = Depends(get_db)) -> dict:
    w = db.get(WorkOrder, wo_id)
    if not w or w.organization_id != p.org_id or not in_scope(p, w.plant_id):
        raise HTTPException(404, "Work order not found")
    out = wo_out(w, db.get(Plant, w.plant_id).name)
    out["events"] = [{"id": str(e.id), "kind": e.kind, "message": e.message, "data": e.data,
                      "user_id": str(e.user_id) if e.user_id else None, "created_at": e.created_at.isoformat()}
                     for e in db.scalars(select(WorkOrderEvent).where(WorkOrderEvent.work_order_id == w.id).order_by(WorkOrderEvent.created_at))]
    return out


@router.patch("/work-orders/{wo_id}")
def update_work_order(wo_id: UUID, body: WorkOrderPatch, request: Request, background: BackgroundTasks,
                      p: Principal = Depends(require("workorders:execute")), db: Session = Depends(get_db)) -> dict:
    w = db.get(WorkOrder, wo_id)
    if not w or w.organization_id != p.org_id or not in_scope(p, w.plant_id):
        raise HTTPException(404, "Work order not found")
    ids = []
    now = datetime.now(UTC)
    if body.assignee_id:
        _validate_refs(db, p, w.plant_id, None, None, body.assignee_id)
        w.assignee_id = body.assignee_id
        db.add(WorkOrderEvent(organization_id=p.org_id, work_order_id=w.id, kind="assigned", user_id=p.user_id,
                              data={"assignee_id": str(body.assignee_id)}))
        if w.status == "open" and not body.status:
            w.status = "assigned"
    for k in ("contractor", "priority", "due_date", "actual_cost"):
        v = getattr(body, k)
        if v is not None:
            setattr(w, k, v)
    if body.status and body.status != w.status:
        if body.status not in TRANSITIONS.get(w.status, set()):
            raise HTTPException(422, f"cannot move work order from {w.status} to {body.status}")
        if body.status == "verified" and p.role == "technician":
            raise HTTPException(403, "Verification must be done by an analyst, plant owner or administrator")
        old = w.status
        w.status = body.status
        if body.status == "in_progress" and not w.started_at:
            w.started_at = now
        if body.status == "completed":
            w.completed_at = now
            ids = queue_event(db, p.org_id, "work_order.completed", wo_out(w))
        db.add(WorkOrderEvent(organization_id=p.org_id, work_order_id=w.id, kind="status_change", user_id=p.user_id,
                              message=body.note, data={"from": old, "to": body.status}))
    elif body.note:
        db.add(WorkOrderEvent(organization_id=p.org_id, work_order_id=w.id, kind="note", message=body.note, user_id=p.user_id))
    audit(db, org_id=p.org_id, user_id=p.user_id, action="workorder.update", entity_type="work_order", entity_id=w.id,
          details=body.model_dump(mode="json", exclude_none=True), ip=client_ip(request))
    db.commit()
    background.add_task(deliver, ids)
    return wo_out(w)


class WOEventIn(BaseModel):
    kind: Literal["note", "inspection", "diagnosis", "repair", "handover", "commissioning", "verification"]
    message: str = Field(max_length=5000)
    data: dict = Field(default_factory=dict)


@router.post("/work-orders/{wo_id}/events", status_code=201)
def add_wo_event(wo_id: UUID, body: WOEventIn, p: Principal = Depends(require("workorders:execute")), db: Session = Depends(get_db)) -> dict:
    w = db.get(WorkOrder, wo_id)
    if not w or w.organization_id != p.org_id or not in_scope(p, w.plant_id):
        raise HTTPException(404, "Work order not found")
    e = WorkOrderEvent(organization_id=p.org_id, work_order_id=w.id, kind=body.kind, message=body.message, data=body.data, user_id=p.user_id)
    db.add(e)
    db.commit()
    return {"id": str(e.id)}


@router.post("/work-orders/{wo_id}/evaluate-recovery")
def evaluate(wo_id: UUID, window_days: int = 14, p: Principal = Depends(require("workorders:write")), db: Session = Depends(get_db)) -> dict:
    """Compare performance before the fault/work order with performance after completion (measured)."""
    w = db.get(WorkOrder, wo_id)
    if not w or w.organization_id != p.org_id or not in_scope(p, w.plant_id):
        raise HTTPException(404, "Work order not found")
    try:
        res = evaluate_recovery(db, w, max(3, min(window_days, 90)))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if res.get("status") == "evaluated":
        w.realised = res
        db.add(WorkOrderEvent(organization_id=p.org_id, work_order_id=w.id, kind="verification", user_id=p.user_id,
                              message="Post-repair performance evaluated", data={"realised_kwh_per_year": res["realised_recovery_kwh_per_year"]}))
        db.commit()
    return res


# --------------------------------------------------------------------------- asset records


class AssetRecordIn(BaseModel):
    plant_id: UUID
    equipment_id: UUID | None = None
    work_order_id: UUID | None = None
    kind: Literal["inspection", "commissioning", "handover", "warranty_claim", "service", "corrective_action", "replacement"]
    title: str = Field(max_length=300)
    performed_by: str | None = None
    contractor: str | None = None
    performed_at: date
    notes: str | None = None
    data: dict = Field(default_factory=dict)


def record_out(r: AssetRecord) -> dict:
    return {"id": str(r.id), "plant_id": str(r.plant_id), "equipment_id": str(r.equipment_id) if r.equipment_id else None,
            "work_order_id": str(r.work_order_id) if r.work_order_id else None, "kind": r.kind, "title": r.title,
            "performed_by": r.performed_by, "contractor": r.contractor, "performed_at": r.performed_at.isoformat(),
            "notes": r.notes, "data": r.data}


@router.get("/asset-records")
def list_records(plant_id: UUID | None = None, equipment_id: UUID | None = None,
                 p: Principal = Depends(require("workorders:read")), db: Session = Depends(get_db)) -> list[dict]:
    q = scoped(select(AssetRecord), AssetRecord.plant_id, p)
    if plant_id:
        q = q.where(AssetRecord.plant_id == plant_id)
    if equipment_id:
        q = q.where(AssetRecord.equipment_id == equipment_id)
    return [record_out(r) for r in db.scalars(q.order_by(AssetRecord.performed_at.desc()))]


@router.post("/asset-records", status_code=201)
def create_record(body: AssetRecordIn, request: Request, p: Principal = Depends(require("workorders:execute")),
                  db: Session = Depends(get_db)) -> dict:
    _validate_refs(db, p, body.plant_id, body.equipment_id, None, None)
    r = AssetRecord(organization_id=p.org_id, **body.model_dump())
    db.add(r)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="asset_record.create", entity_type="asset_record", entity_id=r.id,
          details={"kind": r.kind}, ip=client_ip(request))
    db.commit()
    return record_out(r)


@router.get("/equipment/{equipment_id}/history")
def equipment_history(equipment_id: UUID, p: Principal = Depends(require("workorders:read")), db: Session = Depends(get_db)) -> dict:
    e = db.get(Equipment, equipment_id)
    if not e or e.organization_id != p.org_id or not in_scope(p, e.plant_id):
        raise HTTPException(404, "Equipment not found")
    return {
        "equipment": {"id": str(e.id), "name": e.name, "serial_number": e.serial_number, "warranty_end": e.warranty_end.isoformat() if e.warranty_end else None},
        "records": [record_out(r) for r in db.scalars(select(AssetRecord).where(AssetRecord.equipment_id == e.id).order_by(AssetRecord.performed_at.desc()))],
        "work_orders": [wo_out(w) for w in db.scalars(select(WorkOrder).where(WorkOrder.equipment_id == e.id).order_by(WorkOrder.created_at.desc()))],
        "alerts": [{"id": str(a.id), "title": a.title, "status": a.status, "severity": a.severity, "window_start": a.window_start.isoformat()}
                   for a in db.scalars(select(Alert).where(Alert.equipment_id == e.id).order_by(Alert.window_start.desc()))],
    }
