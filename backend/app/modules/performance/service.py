"""Database-facing performance services: expected-generation model runs and interval loading."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...models import Equipment, ExpectedGeneration, ModelRun, Plant
from . import engine


def model_params(plant: Plant) -> engine.ModelParams:
    return engine.ModelParams(
        dc_kwp=plant.dc_capacity_kwp, ac_kw=plant.ac_capacity_kw, temp_coeff_pct_per_c=plant.temp_coeff_pct_per_c,
        noct_c=plant.noct_c, system_loss_pct=plant.system_loss_pct, annual_degradation_pct=plant.annual_degradation_pct,
        commissioning_date=plant.commissioning_date)


def recompute_expected(db: Session, plant: Plant, start: datetime | None = None, end: datetime | None = None) -> ModelRun:
    """Run the expected-generation model over hourly weather data and replace stored results for the window."""
    q = text("""
        SELECT date_trunc('hour', ts) AS h, avg(poa_wm2) AS poa, avg(ambient_temp_c) AS amb, avg(module_temp_c) AS mod
        FROM weather_readings WHERE plant_id = :p
          AND (CAST(:s AS timestamptz) IS NULL OR ts >= :s) AND (CAST(:e AS timestamptz) IS NULL OR ts < :e)
        GROUP BY 1 ORDER BY 1""")
    rows = db.execute(q, {"p": plant.id, "s": start, "e": end}).all()
    params = model_params(plant)
    run = ModelRun(organization_id=plant.organization_id, plant_id=plant.id, model=engine.MODEL_NAME,
                   model_version=engine.MODEL_VERSION, parameters=params.as_dict(),
                   uncertainty_pct=engine.DEFAULT_UNCERTAINTY_PCT,
                   period_start=rows[0].h if rows else start, period_end=rows[-1].h if rows else end)
    db.add(run)
    db.flush()
    values = []
    for r in rows:
        pt = engine.expected_energy(params, r.poa, r.amb, r.mod, r.h, 1.0)
        if pt is None:
            continue
        values.append({"organization_id": plant.organization_id, "plant_id": plant.id, "ts": r.h,
                       "expected_kwh": pt.expected_kwh, "reference_yield_kwh": pt.reference_yield_kwh,
                       "cell_temp_c": pt.cell_temp_c, "model_run_id": run.id})
    if rows:
        db.execute(delete(ExpectedGeneration).where(ExpectedGeneration.plant_id == plant.id,
                                                    ExpectedGeneration.ts >= rows[0].h, ExpectedGeneration.ts <= rows[-1].h))
    for i in range(0, len(values), 5000):
        stmt = insert(ExpectedGeneration).values(values[i:i + 5000])
        db.execute(stmt.on_conflict_do_update(
            constraint="uq_expected_plant_ts",
            set_={c: stmt.excluded[c] for c in ("expected_kwh", "reference_yield_kwh", "cell_temp_c", "model_run_id")}))
    run.hours_modelled = len(values)
    return run


HOURLY_SQL = text("""
WITH meter AS (
  SELECT date_trunc('hour', ts) AS h, sum(energy_kwh) AS kwh
  FROM energy_readings WHERE plant_id = :p AND equipment_id IS NULL AND ts >= :s AND ts < :e GROUP BY 1
), inv AS (
  SELECT date_trunc('hour', ts) AS h, sum(energy_kwh) AS kwh
  FROM energy_readings WHERE plant_id = :p AND equipment_id IS NOT NULL AND ts >= :s AND ts < :e GROUP BY 1
), wx AS (
  SELECT date_trunc('hour', ts) AS h, avg(poa_wm2) AS poa
  FROM weather_readings WHERE plant_id = :p AND ts >= :s AND ts < :e GROUP BY 1
), hours AS (
  SELECT generate_series(date_trunc('hour', CAST(:s AS timestamptz)), CAST(:e AS timestamptz) - interval '1 hour', interval '1 hour') AS h
)
SELECT hours.h AS ts,
       CASE WHEN :use_meter THEN meter.kwh ELSE inv.kwh END AS actual_kwh,
       eg.expected_kwh, eg.reference_yield_kwh, wx.poa
FROM hours
LEFT JOIN meter ON meter.h = hours.h
LEFT JOIN inv ON inv.h = hours.h
LEFT JOIN wx ON wx.h = hours.h
LEFT JOIN expected_generation eg ON eg.plant_id = :p AND eg.ts = hours.h
ORDER BY hours.h
""")


def data_bounds(db: Session, plant_id: UUID) -> tuple[datetime | None, datetime | None]:
    r = db.execute(text("SELECT min(ts), max(ts) FROM energy_readings WHERE plant_id = :p"), {"p": plant_id}).one()
    return r[0], r[1]


def has_meter(db: Session, plant_id: UUID) -> bool:
    return db.execute(text("SELECT EXISTS (SELECT 1 FROM energy_readings WHERE plant_id = :p AND equipment_id IS NULL)"),
                      {"p": plant_id}).scalar()


def load_intervals(db: Session, plant: Plant, start: datetime, end: datetime) -> list[engine.Interval]:
    """Hourly intervals for [start, end). Night hours (expected 0 / no POA) are kept so gaps stay visible.
    Actual = revenue meter if present, otherwise the sum of inverter meters."""
    rows = db.execute(HOURLY_SQL, {"p": plant.id, "s": start, "e": end, "use_meter": has_meter(db, plant.id)}).all()
    out = []
    for r in rows:
        out.append(engine.Interval(ts=r.ts, actual_kwh=r.actual_kwh, expected_kwh=r.expected_kwh,
                                   reference_yield_kwh=r.reference_yield_kwh, poa_wm2=r.poa))
    return out


def inverter_hourly(db: Session, plant_id: UUID, start: datetime, end: datetime) -> dict[datetime, dict[str, float]]:
    q = text("""SELECT date_trunc('hour', ts) AS h, equipment_id, sum(energy_kwh) AS kwh FROM energy_readings
                WHERE plant_id = :p AND equipment_id IS NOT NULL AND ts >= :s AND ts < :e GROUP BY 1, 2""")
    out: dict[datetime, dict[str, float]] = {}
    for r in db.execute(q, {"p": plant_id, "s": start, "e": end}):
        out.setdefault(r.h, {})[str(r.equipment_id)] = r.kwh
    return out


def inverters(db: Session, plant_id: UUID) -> list[Equipment]:
    return list(db.scalars(select(Equipment).where(Equipment.plant_id == plant_id, Equipment.kind == "inverter")
                           .order_by(Equipment.name)))


def latest_model_run(db: Session, plant_id: UUID) -> ModelRun | None:
    return db.scalar(select(ModelRun).where(ModelRun.plant_id == plant_id).order_by(ModelRun.created_at.desc()).limit(1))


def plant_count_and_capacity(db: Session) -> tuple[int, float, float]:
    r = db.execute(select(func.count(Plant.id), func.coalesce(func.sum(Plant.dc_capacity_kwp), 0),
                          func.coalesce(func.sum(Plant.ac_capacity_kw), 0))).one()
    return int(r[0]), float(r[1]), float(r[2])
