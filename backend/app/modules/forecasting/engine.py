"""Production forecasting (pure).

v1 provides a transparent *climatological* forecast: for each hour-of-day in the
target month, the forecast is the mean of historical actual production for that
hour over the trailing ``lookback_days``, with an empirical P10–P90 band. It
needs no external weather data and acts as the baseline any weather-driven
forecast (e.g. day-ahead irradiance from a licensed provider fed through
``expected_energy``) must beat.

Backtests report MAE, nRMSE and bias against actuals, plus missing-data warnings.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

MODEL = "climatology-hourly"
VERSION = "1.0.0"


@dataclass
class Obs:
    ts: datetime
    actual_kwh: float | None


def _quantile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return xs[f] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)


def climatology_forecast(history: list[Obs], start: datetime, hours: int = 24, lookback_days: int = 30) -> dict:
    cutoff = start - timedelta(days=lookback_days)
    window = [o for o in history if cutoff <= o.ts < start]
    by_hour: dict[int, list[float]] = defaultdict(list)
    missing = 0
    for o in window:
        if o.actual_kwh is None:
            missing += 1
        else:
            by_hour[o.ts.hour].append(o.actual_kwh)
    expected_obs = lookback_days * 24
    coverage = (len(window) - missing) / expected_obs if expected_obs else 0
    warnings = []
    if coverage < 0.9:
        warnings.append(f"Only {coverage:.0%} of the {lookback_days}-day lookback window has production data; "
                        "forecast confidence is reduced.")
    if not window:
        warnings.append("No historical data available before the forecast start — forecast is empty.")
    points = []
    for i in range(hours):
        ts = start + timedelta(hours=i)
        xs = by_hour.get(ts.hour, [])
        points.append({"ts": ts.isoformat(), "p50_kwh": round(statistics.mean(xs), 3) if xs else None,
                       "p10_kwh": round(_quantile(xs, 0.1), 3) if xs else None,
                       "p90_kwh": round(_quantile(xs, 0.9), 3) if xs else None, "samples": len(xs)})
    total = sum(p["p50_kwh"] or 0 for p in points)
    spread = sum((p["p90_kwh"] or 0) - (p["p10_kwh"] or 0) for p in points)
    return {"model": MODEL, "model_version": VERSION, "start": start.isoformat(), "hours": hours,
            "lookback_days": lookback_days, "data_coverage": round(coverage, 4), "total_p50_kwh": round(total, 2),
            "relative_band": round(spread / total, 4) if total else None, "points": points, "warnings": warnings,
            "confidence": "low" if coverage < 0.7 or (total and spread / total > 0.8) else "medium"}


def backtest(history: list[Obs], days: int = 14, lookback_days: int = 30) -> dict:
    """Rolling day-ahead backtest of the climatology model over the last ``days`` days of history."""
    if not history:
        return {"days": 0, "warnings": ["No data"]}
    history = sorted(history, key=lambda o: o.ts)
    end = history[-1].ts.replace(hour=0, minute=0, second=0, microsecond=0)
    actual_by_ts = {o.ts: o.actual_kwh for o in history}
    errs, sq, act_sum, bias, per_day = [], [], 0.0, 0.0, []
    for d in range(days, 0, -1):
        day = end - timedelta(days=d - 1)
        fc = climatology_forecast(history, day, 24, lookback_days)
        f_tot = a_tot = 0.0
        for p in fc["points"]:
            a = actual_by_ts.get(datetime.fromisoformat(p["ts"]))
            if a is None or p["p50_kwh"] is None:
                continue
            e = p["p50_kwh"] - a
            errs.append(abs(e))
            sq.append(e * e)
            bias += e
            act_sum += a
            f_tot += p["p50_kwh"]
            a_tot += a
        per_day.append({"date": day.date().isoformat(), "forecast_kwh": round(f_tot, 2), "actual_kwh": round(a_tot, 2)})
    n = len(errs)
    mean_act = act_sum / n if n else 0
    return {"model": MODEL, "days": days, "hours_scored": n,
            "mae_kwh": round(statistics.mean(errs), 3) if n else None,
            "nrmse": round(math.sqrt(statistics.mean(sq)) / mean_act, 4) if n and mean_act else None,
            "bias_pct": round(bias / act_sum * 100, 2) if act_sum else None, "per_day": per_day}
