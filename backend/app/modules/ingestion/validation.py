"""CSV parsing & validation for energy, weather and event data (pure).

Rules applied to every file:
* required columns present; unknown columns reported as warnings;
* timestamps parsed as ISO-8601; naive timestamps are interpreted in the supplied
  timezone (normally the plant's) and stored in UTC;
* unit-suffixed headers are normalised (``energy_wh``/``energy_mwh`` → kWh);
* physical range checks (non-negative energy, energy ≤ 1.2 × rated power × interval,
  irradiance 0–1600 W/m², temperatures −50…+100 °C);
* duplicate (equipment, timestamp) keys within the file are rejected; duplicates
  against existing data are detected at insert time (unique constraints).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

MAX_ERRORS_REPORTED = 200

ENERGY_UNITS = {"energy_kwh": 1.0, "energy_wh": 0.001, "energy_mwh": 1000.0}
POWER_UNITS = {"power_kw": 1.0, "power_w": 0.001, "power_mw": 1000.0}
EVENT_CATEGORIES = {"outage", "grid_outage", "curtailment", "planned_maintenance", "inverter_fault", "soiling", "other"}

SCHEMAS = {
    "energy": {"required": [["timestamp"], list(ENERGY_UNITS)],
               "optional": ["equipment", "interval_minutes", "quality", *POWER_UNITS]},
    "weather": {"required": [["timestamp"]],
                "optional": ["poa_wm2", "ghi_wm2", "ambient_temp_c", "module_temp_c", "wind_ms"]},
    "events": {"required": [["start"], ["end"], ["category"]],
               "optional": ["equipment", "description", "confirmed"]},
}

RANGES = {
    "poa_wm2": (0, 1600), "ghi_wm2": (0, 1500), "ambient_temp_c": (-50, 70), "module_temp_c": (-50, 100),
    "wind_ms": (0, 80),
}


@dataclass
class RowError:
    row: int
    field: str
    message: str

    def as_dict(self) -> dict:
        return {"row": self.row, "field": self.field, "message": self.message}


@dataclass
class ValidationResult:
    kind: str
    rows: list[dict] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_rows: int = 0
    duplicates_in_file: int = 0

    @property
    def rejected(self) -> int:
        return len({e.row for e in self.errors})

    def summary(self) -> dict:
        return {"kind": self.kind, "total_rows": self.total_rows, "valid_rows": len(self.rows),
                "rejected_rows": self.rejected, "duplicates_in_file": self.duplicates_in_file,
                "warnings": self.warnings, "errors": [e.as_dict() for e in self.errors[:MAX_ERRORS_REPORTED]]}


@dataclass(frozen=True)
class Context:
    timezone: str = "UTC"
    max_kw: float | None = None              # plant AC capacity — upper bound for plant-meter rows
    equipment: dict[str, tuple[str, float | None]] = field(default_factory=dict)  # key(lower) -> (id, rated kW)


def parse_ts(value: str, tz: str) -> datetime:
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(UTC)


def _num(raw: str | None) -> float | None:
    if raw is None or raw.strip() == "":
        return None
    return float(raw.strip())


def validate_csv(text: str, kind: str, ctx: Context) -> ValidationResult:
    if kind not in SCHEMAS:
        raise ValueError(f"unknown import kind {kind!r}")
    res = ValidationResult(kind=kind)
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        res.errors.append(RowError(0, "*", "file is empty or has no header row"))
        return res
    headers = [h.strip().lower() for h in reader.fieldnames]
    reader.fieldnames = headers
    schema = SCHEMAS[kind]
    for alternatives in schema["required"]:
        if not any(a in headers for a in alternatives):
            res.errors.append(RowError(0, "/".join(alternatives), "required column missing"))
    if res.errors:
        return res
    known = {a for alts in schema["required"] for a in alts} | set(schema["optional"])
    unknown = [h for h in headers if h not in known]
    if unknown:
        res.warnings.append(f"ignored unknown columns: {', '.join(unknown)}")
    seen: set[tuple] = set()
    for idx, raw in enumerate(reader, start=2):  # row 1 is the header
        res.total_rows += 1
        try:
            row = _VALIDATORS[kind](raw, ctx, idx, res)
        except _RowRejected:
            continue
        key = row.get("_key")
        if key in seen:
            res.duplicates_in_file += 1
            res.errors.append(RowError(idx, "timestamp", "duplicate timestamp in file"))
            continue
        seen.add(key)
        row.pop("_key", None)
        res.rows.append(row)
    return res


class _RowRejected(Exception):
    pass


def _fail(res: ValidationResult, row: int, fld: str, msg: str) -> None:
    res.errors.append(RowError(row, fld, msg))
    raise _RowRejected


def _equipment(raw: dict, ctx: Context, idx: int, res: ValidationResult) -> tuple[str | None, float | None]:
    name = (raw.get("equipment") or "").strip()
    if not name:
        return None, None
    hit = ctx.equipment.get(name.lower())
    if hit is None:
        _fail(res, idx, "equipment", f"unknown equipment '{name}' (use serial number or name)")
    return hit


def _energy(raw: dict, ctx: Context, idx: int, res: ValidationResult) -> dict:
    try:
        ts = parse_ts(raw["timestamp"], ctx.timezone)
    except (ValueError, TypeError, AttributeError):
        _fail(res, idx, "timestamp", f"invalid timestamp {raw.get('timestamp')!r}")
    col = next(c for c in ENERGY_UNITS if c in raw)
    try:
        val = _num(raw.get(col))
    except ValueError:
        _fail(res, idx, col, f"not a number: {raw.get(col)!r}")
    if val is None:
        _fail(res, idx, col, "missing energy value")
    kwh = val * ENERGY_UNITS[col]
    try:
        interval = int(raw.get("interval_minutes") or 60)
    except ValueError:
        _fail(res, idx, "interval_minutes", "must be an integer")
    if interval not in (1, 5, 10, 15, 30, 60):
        _fail(res, idx, "interval_minutes", "supported intervals: 1, 5, 10, 15, 30, 60")
    eq_id, rated = _equipment(raw, ctx, idx, res)
    if kwh < 0:
        _fail(res, idx, col, "negative energy")
    cap = rated if eq_id else ctx.max_kw
    if cap and kwh > 1.2 * cap * interval / 60:
        _fail(res, idx, col, f"{kwh:.1f} kWh exceeds 120% of rated {cap:.0f} kW over {interval} min")
    power = None
    pcol = next((c for c in POWER_UNITS if c in raw), None)
    if pcol:
        try:
            p = _num(raw.get(pcol))
            power = p * POWER_UNITS[pcol] if p is not None else None
        except ValueError:
            _fail(res, idx, pcol, "not a number")
    quality = (raw.get("quality") or "ok").strip().lower() or "ok"
    if quality not in {"ok", "estimated", "suspect"}:
        _fail(res, idx, "quality", "quality must be ok, estimated or suspect")
    return {"_key": (eq_id, ts), "ts": ts, "energy_kwh": kwh, "power_kw": power, "equipment_id": eq_id,
            "interval_minutes": interval, "quality": quality}


def _weather(raw: dict, ctx: Context, idx: int, res: ValidationResult) -> dict:
    try:
        ts = parse_ts(raw["timestamp"], ctx.timezone)
    except (ValueError, TypeError, AttributeError):
        _fail(res, idx, "timestamp", f"invalid timestamp {raw.get('timestamp')!r}")
    out = {"_key": ts, "ts": ts}
    any_val = False
    for f in SCHEMAS["weather"]["optional"]:
        try:
            v = _num(raw.get(f))
        except ValueError:
            _fail(res, idx, f, f"not a number: {raw.get(f)!r}")
        if v is not None:
            lo, hi = RANGES[f]
            if not lo <= v <= hi:
                _fail(res, idx, f, f"{v} outside plausible range [{lo}, {hi}]")
            any_val = True
        out[f] = v
    if not any_val:
        _fail(res, idx, "*", "row has no measurements")
    return out


def _events(raw: dict, ctx: Context, idx: int, res: ValidationResult) -> dict:
    try:
        start, end = parse_ts(raw["start"], ctx.timezone), parse_ts(raw["end"], ctx.timezone)
    except (ValueError, TypeError, AttributeError):
        _fail(res, idx, "start/end", "invalid timestamp")
    if end <= start:
        _fail(res, idx, "end", "end must be after start")
    cat = (raw.get("category") or "").strip().lower()
    if cat not in EVENT_CATEGORIES:
        _fail(res, idx, "category", f"must be one of {sorted(EVENT_CATEGORIES)}")
    eq_id, _ = _equipment(raw, ctx, idx, res)
    confirmed = (raw.get("confirmed") or "").strip().lower() in {"1", "true", "yes", "y"}
    return {"_key": (eq_id, cat, start), "start_ts": start, "end_ts": end, "category": cat, "equipment_id": eq_id,
            "description": (raw.get("description") or "").strip() or None, "confirmed": confirmed}


_VALIDATORS = {"energy": _energy, "weather": _weather, "events": _events}
