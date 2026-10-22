"""CSV import (with validation, duplicate detection, source tracking) and CSV export."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...config import settings
from ...core.audit import audit
from ...core.deps import Principal, client_ip, require
from ...db import get_db
from ...models import DataSource, EnergyReading, Equipment, ImportBatch, OperationalEvent, WeatherReading
from ..common import get_plant, resolve_period
from ..integrations.webhooks import deliver, queue_event
from ..performance.service import recompute_expected
from .validation import Context, validate_csv

router = APIRouter(tags=["data"])


def _source(db: Session, org_id: UUID, name: str) -> DataSource:
    src = db.scalar(select(DataSource).where(DataSource.kind == "csv_upload", DataSource.name == name))
    if src is None:
        src = DataSource(organization_id=org_id, name=name, kind="csv_upload", provider="Customer upload",
                         license_notes="Customer-owned operational data", coverage_notes="Single plant")
        db.add(src)
        db.flush()
    src.retrieved_at = datetime.now(UTC)
    return src


def batch_out(b: ImportBatch) -> dict:
    return {"id": str(b.id), "plant_id": str(b.plant_id), "kind": b.kind, "filename": b.filename, "status": b.status,
            "sha256": b.sha256, "rows_total": b.rows_total, "rows_imported": b.rows_imported, "rows_duplicate": b.rows_duplicate,
            "rows_rejected": b.rows_rejected, "errors": b.errors[:50], "created_at": b.created_at.isoformat() if b.created_at else None}


@router.post("/plants/{plant_id}/imports")
async def import_csv(plant_id: UUID, request: Request, background: BackgroundTasks,
                     kind: Literal["energy", "weather", "events"] = Query(...),
                     dry_run: bool = Query(False, description="Validate only; nothing is stored"),
                     timezone: str | None = Query(None, description="Timezone for naive timestamps (default: plant timezone)"),
                     source_name: str = Query("CSV upload", max_length=200),
                     file: UploadFile = File(...),
                     p: Principal = Depends(require("data:import")), db: Session = Depends(get_db)) -> dict:
    plant = get_plant(db, plant_id, p)
    raw = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 encoded CSV") from None
    sha = hashlib.sha256(raw).hexdigest()
    eq = {}
    for e in db.scalars(select(Equipment).where(Equipment.plant_id == plant.id)):
        for key in filter(None, (e.serial_number, e.name)):
            eq[key.lower()] = (str(e.id), e.rated_power_kw)
    ctx = Context(timezone=timezone or plant.timezone, max_kw=plant.ac_capacity_kw, equipment=eq)
    try:
        result = validate_csv(content, kind, ctx)
    except Exception as exc:  # malformed CSV structure
        raise HTTPException(422, f"Could not parse CSV: {exc}") from None
    summary = result.summary()
    if any(e.row == 0 for e in result.errors):  # structural problem (missing header / required column)
        raise HTTPException(422, {"message": "CSV structure invalid", **summary})
    previous = db.scalar(select(ImportBatch).where(ImportBatch.plant_id == plant.id, ImportBatch.sha256 == sha,
                                                   ImportBatch.status == "completed"))
    if previous:
        summary["warnings"].append(f"Identical file already imported on {previous.created_at:%Y-%m-%d} (batch {previous.id}).")
    if dry_run:
        return {"dry_run": True, **summary}
    src = _source(db, p.org_id, source_name)
    batch = ImportBatch(organization_id=p.org_id, plant_id=plant.id, source_id=src.id, filename=file.filename, kind=kind,
                        sha256=sha, rows_total=result.total_rows, rows_rejected=result.rejected,
                        errors=[e.as_dict() for e in result.errors[:500]], created_by=p.user_id, status="completed")
    db.add(batch)
    db.flush()
    imported = 0
    common = {"organization_id": p.org_id, "plant_id": plant.id}
    if kind == "events":
        for r in result.rows:
            db.add(OperationalEvent(**common, source="import", **r))
            imported += 1
    else:
        model = EnergyReading if kind == "energy" else WeatherReading
        rows = [{**common, "source_id": src.id, "batch_id": batch.id, **r} for r in result.rows]
        for i in range(0, len(rows), 5000):
            stmt = insert(model).values(rows[i:i + 5000]).on_conflict_do_nothing().returning(model.id)
            imported += len(db.execute(stmt).all())
    batch.rows_imported = imported
    batch.rows_duplicate = len(result.rows) - imported if kind != "events" else 0
    batch.rows_duplicate += result.duplicates_in_file
    model_run = None
    if kind == "weather" and imported:
        times = [r["ts"] for r in result.rows]
        model_run = recompute_expected(db, plant, min(times), max(times) + timedelta(hours=1))
    audit(db, org_id=p.org_id, user_id=p.user_id, action="data.import", entity_type="import_batch", entity_id=batch.id,
          details={"kind": kind, "imported": imported, "rejected": result.rejected, "file": file.filename}, ip=client_ip(request))
    ids = queue_event(db, p.org_id, "import.completed", batch_out(batch))
    db.commit()
    background.add_task(deliver, ids)
    out = {**batch_out(batch), "warnings": summary["warnings"], "duplicates_existing": len(result.rows) - imported if kind != "events" else 0}
    if model_run:
        out["model_run"] = {"id": str(model_run.id), "hours_modelled": model_run.hours_modelled}
    return out


@router.get("/plants/{plant_id}/imports")
def list_imports(plant_id: UUID, p: Principal = Depends(require("plants:read")), db: Session = Depends(get_db)) -> list[dict]:
    get_plant(db, plant_id, p)
    return [batch_out(b) for b in db.scalars(select(ImportBatch).where(ImportBatch.plant_id == plant_id)
                                             .order_by(ImportBatch.created_at.desc()).limit(100))]


EXPORT_SQL = {
    "energy": """SELECT e.ts, e.energy_kwh, e.power_kw, e.interval_minutes, coalesce(q.serial_number, q.name) AS equipment,
                        e.quality, s.name AS source FROM energy_readings e LEFT JOIN equipment q ON q.id = e.equipment_id
                        LEFT JOIN data_sources s ON s.id = e.source_id
                 WHERE e.plant_id = :p AND e.ts >= :s AND e.ts < :e ORDER BY e.ts, equipment""",
    "weather": """SELECT w.ts, w.poa_wm2, w.ghi_wm2, w.ambient_temp_c, w.module_temp_c, w.wind_ms, s.name AS source
                  FROM weather_readings w LEFT JOIN data_sources s ON s.id = w.source_id
                  WHERE w.plant_id = :p AND w.ts >= :s AND w.ts < :e ORDER BY w.ts""",
    "expected": """SELECT g.ts, g.expected_kwh, g.reference_yield_kwh, g.cell_temp_c, m.model, m.model_version
                   FROM expected_generation g JOIN model_runs m ON m.id = g.model_run_id
                   WHERE g.plant_id = :p AND g.ts >= :s AND g.ts < :e ORDER BY g.ts""",
}


@router.get("/plants/{plant_id}/exports/{dataset}.csv")
def export_csv(plant_id: UUID, dataset: Literal["energy", "weather", "expected"], start: datetime | None = None,
               end: datetime | None = None, p: Principal = Depends(require("data:export")), db: Session = Depends(get_db)):
    plant = get_plant(db, plant_id, p)
    s, e = resolve_period(db, [plant.id], start, end, default_days=365)
    result = db.execute(text(EXPORT_SQL[dataset]), {"p": plant.id, "s": s, "e": e})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# plant", plant.code, "units: kWh, kW, W/m2, degC, m/s; timestamps UTC interval start"])
    w.writerow(list(result.keys()))
    for row in result:
        w.writerow([v.isoformat() if isinstance(v, datetime) else v for v in row])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{plant.code}_{dataset}.csv"'})
