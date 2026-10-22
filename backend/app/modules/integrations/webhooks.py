"""Outbound webhooks: HMAC-SHA256 signed JSON POSTs with a delivery log.

Header ``X-Yala-Signature: sha256=<hex>`` is computed over the raw body with the
subscription secret. Receivers must verify it and should treat deliveries as
at-least-once (use ``X-Yala-Delivery`` for idempotency).

Failed deliveries are retried by the scheduler with exponential backoff
(1 min, 5 min, 30 min, 2 h, 12 h) and abandoned after ``MAX_ATTEMPTS``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...db import system_session
from ...models import WebhookDelivery, WebhookSubscription

EVENTS = {"alert.created", "work_order.created", "work_order.completed", "import.completed"}
log = logging.getLogger("yala.webhooks")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def queue_event(db: Session, org_id: UUID, event: str, payload: dict) -> list[UUID]:
    """Create pending deliveries in the caller's transaction; send with ``deliver`` after commit."""
    subs = db.scalars(select(WebhookSubscription).where(WebhookSubscription.organization_id == org_id,
                                                        WebhookSubscription.is_active.is_(True))).all()
    ids = []
    for s in subs:
        if event in s.events or "*" in s.events:
            d = WebhookDelivery(organization_id=org_id, subscription_id=s.id, event=event, payload=payload)
            db.add(d)
            db.flush()
            ids.append(d.id)
    return ids


MAX_ATTEMPTS = 6
BACKOFF = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=12)]


def _attempt(db: Session, d: WebhookDelivery, sub: WebhookSubscription) -> None:
    body = json.dumps({"event": d.event, "delivery_id": str(d.id), "data": d.payload}, default=str).encode()
    d.attempts += 1
    d.attempted_at = datetime.now(UTC)
    d.error = None
    try:
        r = httpx.post(sub.url, content=body, timeout=settings.external_http_timeout, headers={
            "Content-Type": "application/json", "X-Yala-Event": d.event, "X-Yala-Delivery": str(d.id),
            "X-Yala-Attempt": str(d.attempts), "X-Yala-Signature": sign(sub.secret, body)})
        d.status_code, d.success = r.status_code, 200 <= r.status_code < 300
        if not d.success:
            d.error = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        d.success, d.error = False, str(exc)[:500]
    if d.success or d.attempts >= MAX_ATTEMPTS or not sub.is_active:
        d.next_attempt_at = None
    else:
        d.next_attempt_at = datetime.now(UTC) + BACKOFF[min(d.attempts - 1, len(BACKOFF) - 1)]
    if not d.success:
        log.warning("webhook delivery failed", extra={"extra_fields": {"delivery": str(d.id), "attempt": d.attempts,
                                                                       "error": d.error}})


def deliver(delivery_ids: list[UUID]) -> None:
    if not delivery_ids:
        return
    with system_session() as db:
        for did in delivery_ids:
            d = db.get(WebhookDelivery, did)
            sub = db.get(WebhookSubscription, d.subscription_id) if d else None
            if d and sub:
                _attempt(db, d, sub)
                db.commit()


def retry_due(limit: int = 100) -> int:
    """Re-send failed deliveries whose backoff has elapsed. Returns the number attempted."""
    with system_session() as db:
        due = db.scalars(select(WebhookDelivery).where(WebhookDelivery.success.is_(False),
                                                       WebhookDelivery.next_attempt_at <= datetime.now(UTC))
                         .order_by(WebhookDelivery.next_attempt_at).limit(limit).with_for_update(skip_locked=True)).all()
        for d in due:
            sub = db.get(WebhookSubscription, d.subscription_id)
            if sub:
                _attempt(db, d, sub)
            else:
                d.next_attempt_at = None
        db.commit()
        return len(due)
