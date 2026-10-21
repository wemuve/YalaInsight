from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.audit import audit
from ...core.deps import Principal, client_ip, require, require_module
from ...db import get_db
from ...models import DataSource, WebhookDelivery, WebhookSubscription
from ..common import get_plant, plant_geo
from .connectors.base import ConnectorError
from .connectors.registry import RESOURCE_CONNECTORS, catalog
from .webhooks import EVENTS, deliver, queue_event

router = APIRouter(tags=["integrations"])
gated = [Depends(require_module("integrations"))]


@router.get("/integrations/connectors")
def connectors(p: Principal = Depends(require("plants:read"))) -> list[dict]:
    return catalog()


@router.post("/plants/{plant_id}/baselines/{provider}", dependencies=gated)
def fetch_baseline(plant_id: UUID, provider: str, request: Request, p: Principal = Depends(require("data:import")),
                   db: Session = Depends(get_db)) -> dict:
    """Retrieve a long-term expected-yield baseline from an external resource model and store it with provenance."""
    plant = get_plant(db, plant_id, p)
    cls = RESOURCE_CONNECTORS.get(provider)
    if not cls:
        raise HTTPException(404, f"unknown provider; available: {list(RESOURCE_CONNECTORS)}")
    g = plant_geo(db, plant.id)
    try:
        est = cls().estimate(g["lat"], g["lon"], plant.dc_capacity_kwp, plant.system_loss_pct, plant.tilt_deg, plant.azimuth_deg)
    except ConnectorError as exc:
        raise HTTPException(502, str(exc)) from None
    m = est.metadata
    src = DataSource(organization_id=p.org_id, name=f"{m.provider} baseline — {plant.code}", kind=m.kind, provider=m.provider,
                     url=m.url, license_notes=m.license_notes, coverage_notes=m.coverage_notes, model_version=m.model_version,
                     retrieved_at=est.retrieved_at, meta={"plant_id": str(plant.id), "inputs": est.inputs,
                                                          "monthly_kwh": est.monthly_kwh, "annual_kwh": est.annual_kwh})
    db.add(src)
    plant.meta = {**(plant.meta or {}), f"baseline_{m.kind}": {"annual_kwh": est.annual_kwh, "monthly_kwh": est.monthly_kwh,
                                                              "retrieved_at": est.retrieved_at.isoformat(), "model_version": m.model_version}}
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="baseline.fetch", entity_type="data_source", entity_id=src.id,
          details={"provider": provider}, ip=client_ip(request))
    db.commit()
    return {"source_id": str(src.id), "provider": m.provider, "model_version": m.model_version, "annual_kwh": est.annual_kwh,
            "monthly_kwh": est.monthly_kwh, "specific_yield_kwh_per_kwp": est.annual_kwh / plant.dc_capacity_kwp,
            "license_notes": m.license_notes, "coverage_notes": m.coverage_notes, "basis": "modelled",
            "note": "Long-term typical-year estimate — a baseline for expectations, not evidence of actual equipment condition."}


class WebhookIn(BaseModel):
    url: str = Field(max_length=2000)
    events: list[str]

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://") and not v.startswith("http://localhost"):
            raise ValueError("webhook URL must use https")
        return v


def hook_out(h: WebhookSubscription, secret: bool = False) -> dict:
    d = {"id": str(h.id), "url": h.url, "events": h.events, "is_active": h.is_active, "created_at": h.created_at.isoformat()}
    if secret:
        d["secret"] = h.secret
    return d


@router.get("/webhooks", dependencies=gated)
def list_hooks(p: Principal = Depends(require("webhooks:manage")), db: Session = Depends(get_db)) -> list[dict]:
    return [hook_out(h) for h in db.scalars(select(WebhookSubscription))]


@router.post("/webhooks", status_code=201, dependencies=gated)
def create_hook(body: WebhookIn, request: Request, p: Principal = Depends(require("webhooks:manage")), db: Session = Depends(get_db)) -> dict:
    bad = [e for e in body.events if e not in EVENTS and e != "*"]
    if bad:
        raise HTTPException(422, f"unknown events {bad}; available {sorted(EVENTS)}")
    h = WebhookSubscription(organization_id=p.org_id, url=body.url, events=body.events, secret=secrets.token_hex(32))
    db.add(h)
    db.flush()
    audit(db, org_id=p.org_id, user_id=p.user_id, action="webhook.create", entity_type="webhook", entity_id=h.id,
          details={"url": body.url, "events": body.events}, ip=client_ip(request))
    db.commit()
    return hook_out(h, secret=True)


@router.delete("/webhooks/{hook_id}", status_code=204, dependencies=gated)
def delete_hook(hook_id: UUID, p: Principal = Depends(require("webhooks:manage")), db: Session = Depends(get_db)) -> None:
    h = db.get(WebhookSubscription, hook_id)
    if not h or h.organization_id != p.org_id:
        raise HTTPException(404, "Webhook not found")
    db.delete(h)
    db.commit()


@router.post("/webhooks/{hook_id}/test", dependencies=gated)
def test_hook(hook_id: UUID, background: BackgroundTasks, p: Principal = Depends(require("webhooks:manage")),
              db: Session = Depends(get_db)) -> dict:
    h = db.get(WebhookSubscription, hook_id)
    if not h or h.organization_id != p.org_id:
        raise HTTPException(404, "Webhook not found")
    d = WebhookDelivery(organization_id=p.org_id, subscription_id=h.id, event="test", payload={"ping": datetime.now(UTC).isoformat()})
    db.add(d)
    db.commit()
    background.add_task(deliver, [d.id])
    return {"delivery_id": str(d.id)}


@router.get("/webhooks/deliveries", dependencies=gated)
def deliveries(p: Principal = Depends(require("webhooks:manage")), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": str(d.id), "subscription_id": str(d.subscription_id), "event": d.event, "status_code": d.status_code,
             "success": d.success, "error": d.error, "attempted_at": d.attempted_at.isoformat()}
            for d in db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.attempted_at.desc()).limit(100))]


__all__ = ["router", "queue_event"]
