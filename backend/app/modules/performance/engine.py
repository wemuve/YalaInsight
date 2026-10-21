"""Expected-production model and performance KPIs (pure functions, no I/O).

Model ``yala-poa-temperature`` v1:

    reference_yield_kWh = P_dc[kWp] × POA[W/m²] / 1000 × Δt[h]
    T_cell              = T_module + 3 × POA/1000              (if back-of-module sensor)
                        = T_ambient + (NOCT − 20) / 800 × POA   (otherwise)
    dc_kWh              = reference_yield × (1 + γ/100 × (T_cell − 25)) × (1 − L_sys/100) × D(age)
    expected_kWh        = min(dc_kWh, P_ac[kW] × Δt)            (inverter clipping)
    D(age)              = (1 − d/100) ^ years_since_commissioning

PR (IEC 61724-1 style, uncorrected) = Σ actual_kWh / Σ reference_yield_kWh.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

MODEL_NAME = "yala-poa-temperature"
MODEL_VERSION = "1.0.0"
DAYLIGHT_POA_WM2 = 50.0  # below this POA an interval is not counted for availability
DEFAULT_UNCERTAINTY_PCT = 5.0  # on-site calibrated POA pyranometer; satellite / PVGIS baselines should use ~8-10 %


@dataclass(frozen=True)
class ModelParams:
    dc_kwp: float
    ac_kw: float
    temp_coeff_pct_per_c: float = -0.37
    noct_c: float = 45.0
    system_loss_pct: float = 14.0
    annual_degradation_pct: float = 0.5
    commissioning_date: date | None = None

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["commissioning_date"] = self.commissioning_date.isoformat() if self.commissioning_date else None
        return d


@dataclass(frozen=True)
class ExpectedPoint:
    expected_kwh: float
    reference_yield_kwh: float
    cell_temp_c: float | None


def cell_temperature(poa_wm2: float, ambient_c: float | None, module_c: float | None, noct_c: float) -> float | None:
    if module_c is not None:
        return module_c + 3.0 * poa_wm2 / 1000.0
    if ambient_c is not None:
        return ambient_c + (noct_c - 20.0) / 800.0 * poa_wm2
    return None


def degradation_factor(commissioning: date | None, at: date, annual_pct: float) -> float:
    if commissioning is None or at <= commissioning:
        return 1.0
    years = (at - commissioning).days / 365.25
    return (1.0 - annual_pct / 100.0) ** years


def expected_energy(p: ModelParams, poa_wm2: float | None, ambient_c: float | None, module_c: float | None,
                    at: datetime, interval_h: float = 1.0) -> ExpectedPoint | None:
    """Expected AC energy for one interval, or None when irradiance is missing (no baseline possible)."""
    if poa_wm2 is None or poa_wm2 < 0:
        return None
    ref = p.dc_kwp * poa_wm2 / 1000.0 * interval_h
    tcell = cell_temperature(poa_wm2, ambient_c, module_c, p.noct_c)
    temp_factor = 1.0 + p.temp_coeff_pct_per_c / 100.0 * ((tcell if tcell is not None else 25.0) - 25.0)
    dc = ref * temp_factor * (1.0 - p.system_loss_pct / 100.0) * degradation_factor(
        p.commissioning_date, at.date(), p.annual_degradation_pct)
    ac = max(0.0, min(dc, p.ac_kw * interval_h))
    return ExpectedPoint(expected_kwh=ac, reference_yield_kwh=ref, cell_temp_c=tcell)


# --------------------------------------------------------------------------- KPIs


@dataclass
class Interval:
    ts: datetime
    actual_kwh: float | None
    expected_kwh: float | None
    reference_yield_kwh: float | None
    poa_wm2: float | None = None


def _ratio(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def period_key(ts: datetime, granularity: str) -> str:
    if granularity == "hour":
        return ts.strftime("%Y-%m-%dT%H:00")
    if granularity == "day":
        return ts.strftime("%Y-%m-%d")
    if granularity == "month":
        return ts.strftime("%Y-%m")
    if granularity == "year":
        return ts.strftime("%Y")
    raise ValueError(f"unsupported granularity: {granularity}")


def summarize(intervals: Iterable[Interval], dc_kwp: float) -> dict:
    """KPIs over a set of intervals. Intervals lacking either actual or expected are excluded from
    comparisons and reported as data gaps rather than silently treated as zero."""
    actual_all = expected_cmp = actual_cmp = ref = 0.0
    n = n_cmp = daylight = producing = missing_actual = missing_expected = 0
    for iv in intervals:
        n += 1
        if iv.actual_kwh is not None:
            actual_all += iv.actual_kwh
        else:
            missing_actual += 1
        if iv.expected_kwh is None:
            missing_expected += 1
        if iv.actual_kwh is not None and iv.expected_kwh is not None:
            n_cmp += 1
            actual_cmp += iv.actual_kwh
            expected_cmp += iv.expected_kwh
            ref += iv.reference_yield_kwh or 0.0
            if (iv.poa_wm2 or 0) >= DAYLIGHT_POA_WM2:
                daylight += 1
                if iv.actual_kwh > 0.01 * max(iv.expected_kwh, 1e-9):
                    producing += 1
    variance = actual_cmp - expected_cmp
    return {
        "intervals": n,
        "intervals_compared": n_cmp,
        "missing_actual_intervals": missing_actual,
        "missing_expected_intervals": missing_expected,
        "data_coverage": _ratio(n_cmp, n),
        "actual_kwh": actual_all,
        "actual_kwh_compared": actual_cmp,
        "expected_kwh": expected_cmp,
        "variance_kwh": variance,
        "variance_pct": _ratio(variance, expected_cmp) * 100 if expected_cmp > 0 else None,
        "performance_ratio": _ratio(actual_cmp, ref),
        "performance_index": _ratio(actual_cmp, expected_cmp),  # actual / temperature- & loss-adjusted expected
        "availability": _ratio(producing, daylight),
        "specific_yield_kwh_per_kwp": actual_all / dc_kwp if dc_kwp else None,
    }


def aggregate(intervals: Iterable[Interval], granularity: str, dc_kwp: float) -> list[dict]:
    buckets: dict[str, list[Interval]] = defaultdict(list)
    for iv in intervals:
        buckets[period_key(iv.ts, granularity)].append(iv)
    return [{"period": k, **summarize(v, dc_kwp)} for k, v in sorted(buckets.items())]
