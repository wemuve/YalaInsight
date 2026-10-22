"""Background jobs.

Every app instance runs a lightweight scheduler thread when ``YALA_SCHEDULER_ENABLED=true``. Each job is
guarded by a PostgreSQL advisory lock, so with N instances (or N workers) a job still runs once per
interval. Run history is kept in ``scheduled_jobs``; failures are logged and retried next interval.

Run a job by hand:  python -m app.jobs anomaly_detection
"""
from __future__ import annotations

import logging
import sys
import threading
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from .config import settings
from .db import engine, system_session
from .licensing import enabled_modules
from .models import Organization, Plant, ScheduledJob

log = logging.getLogger("yala.jobs")


def job_webhook_retry() -> dict:
    from .modules.integrations.webhooks import retry_due
    return {"retried": retry_due()}


def job_anomaly_detection() -> dict:
    """Re-run detection over the last 3 days of data for every operational plant of licensed organisations."""
    from .modules.anomaly.service import detect
    from .modules.integrations.webhooks import deliver
    from .modules.performance.service import data_bounds

    plants = created = 0
    deliveries: list = []
    with system_session() as db:
        orgs = [o for o in db.scalars(select(Organization).where(Organization.is_active.is_(True)))
                if "anomaly" in enabled_modules(db, o)]
        for org in orgs:
            for plant in db.scalars(select(Plant).where(Plant.organization_id == org.id, Plant.status == "operational")):
                _, last = data_bounds(db, plant.id)
                if last is None:
                    continue
                end = last + timedelta(hours=1)
                try:
                    res = detect(db, plant, end - timedelta(days=3), end)
                    db.commit()
                except Exception:  # noqa: BLE001 — one plant's failure must not stop the others
                    db.rollback()
                    log.exception("detection failed", extra={"extra_fields": {"plant": str(plant.id)}})
                    continue
                plants += 1
                created += res["created"]
                deliveries += res["delivery_ids"]
    deliver(deliveries)
    return {"plants": plants, "alerts_created": created}


JOBS: dict[str, tuple[Callable[[], int], Callable[[], dict]]] = {
    "webhook_retry": (lambda: 60, job_webhook_retry),
    "anomaly_detection": (lambda: settings.detection_interval_minutes * 60, job_anomaly_detection),
}


def run_job(name: str, force: bool = False) -> dict | None:
    """Run one job if it is due (or ``force``) and no other instance holds its lock."""
    interval, fn = JOBS[name]
    key = zlib.crc32(f"yala-job:{name}".encode())
    with engine.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar():
            return None
        try:
            now = datetime.now(UTC)
            with system_session() as db:
                rec = db.get(ScheduledJob, name) or ScheduledJob(name=name, details={})
                if not force and rec.last_started_at and rec.last_started_at > now - timedelta(seconds=interval()):
                    return None
                rec.last_started_at, rec.last_status = now, "running"
                db.merge(rec)
                db.commit()
            status, details = "ok", {}
            try:
                details = fn()
            except Exception as exc:  # noqa: BLE001
                status, details = "failed", {"error": f"{type(exc).__name__}: {exc}"[:500]}
                log.exception("job failed", extra={"extra_fields": {"job": name}})
            with system_session() as db:
                rec = db.get(ScheduledJob, name)
                rec.last_finished_at, rec.last_status, rec.details = datetime.now(UTC), status, details
                db.commit()
            log.info("job finished", extra={"extra_fields": {"job": name, "status": status, **details}})
            return {"status": status, **details}
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            lock_conn.commit()


class Scheduler(threading.Thread):
    def __init__(self, tick_seconds: float = 30.0) -> None:
        super().__init__(daemon=True, name="yala-scheduler")
        self.tick = tick_seconds
        self._halt = threading.Event()

    def run(self) -> None:
        log.info("scheduler started")
        while not self._halt.wait(self.tick):
            for name in JOBS:
                try:
                    run_job(name)
                except Exception:  # noqa: BLE001 — e.g. database briefly unavailable
                    log.exception("scheduler tick failed", extra={"extra_fields": {"job": name}})

    def stop(self) -> None:
        self._halt.set()


def job_status() -> list[dict]:
    with system_session() as db:
        return [{"name": j.name, "last_started_at": j.last_started_at.isoformat() if j.last_started_at else None,
                 "last_finished_at": j.last_finished_at.isoformat() if j.last_finished_at else None,
                 "last_status": j.last_status, "details": j.details} for j in db.scalars(select(ScheduledJob))]


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in JOBS:
        sys.exit(f"usage: python -m app.jobs {{{'|'.join(JOBS)}}}")
    print(run_job(sys.argv[1], force=True))
