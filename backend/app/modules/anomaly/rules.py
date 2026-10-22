"""Transparent, rule-based anomaly detection (pure functions).

Every rule is deterministic, versioned and returns the evidence it used. Findings
are *flags for investigation*, never confirmed faults: a finding only becomes
"confirmed" when a person records an inspection outcome against the alert.

Machine-learning detectors can be added later as additional rules that return
the same ``Finding`` structure once enough validated (confirmed / false-positive)
alerts exist to train and evaluate them.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DISCLAIMER = ("Automated anomaly flag — not a confirmed fault. Confirm by inspection or additional measurements "
              "before attributing cause.")

SEVERITY_WEIGHT = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class HourPoint:
    ts: datetime
    actual_kwh: float | None
    expected_kwh: float | None
    poa_wm2: float | None
    excluded: bool = False  # covered by a known curtailment / grid outage / planned maintenance event
    inverters: dict[str, float | None] = field(default_factory=dict)  # equipment_id -> kWh


@dataclass
class InverterInfo:
    id: str
    name: str
    dc_kwp: float


@dataclass
class Finding:
    rule_id: str
    rule_version: str
    category: str
    severity: str
    confidence: str
    title: str
    explanation: str
    evidence: dict
    recommended_actions: list[str]
    window_start: datetime
    window_end: datetime
    energy_loss_kwh: float
    equipment_id: str | None = None

    @property
    def dedup_key(self) -> str:
        return f"{self.rule_id}:{self.equipment_id or 'plant'}:{self.window_start.isoformat()}"


@dataclass(frozen=True)
class RuleConfig:
    irradiance_threshold_wm2: float = 200.0
    zero_output_ratio: float = 0.02
    min_run_hours: int = 2
    inverter_peer_ratio: float = 0.5
    underperf_deficit: float = 0.10
    low_pi_threshold: float = 0.90
    low_pi_days_required: int = 5
    low_pi_window_days: int = 7
    flatline_hours: int = 4
    gap_hours: int = 3
    trend_min_days: int = 14
    trend_slope_per_day: float = -0.003
    trend_min_r2: float = 0.5


def _runs(flags: list[bool | None]) -> list[tuple[int, int]]:
    """Return [start, end] index pairs (inclusive) of consecutive True values.

    ``None`` is neutral (e.g. night hours): it neither extends nor breaks a run, so a fault that
    persists over several days is reported once. Runs start and end on True values."""
    runs, start, last_true = [], None, None
    for i, f in enumerate(flags):
        if f:
            if start is None:
                start = i
            last_true = i
        elif f is False and start is not None:
            runs.append((start, last_true))
            start = None
    if start is not None:
        runs.append((start, last_true))
    return runs


def _true_count(flags: list[bool | None], s: int, e: int) -> int:
    return sum(1 for f in flags[s:e + 1] if f)


def _daylight(p: HourPoint, cfg: RuleConfig) -> bool:
    return (p.poa_wm2 or 0) >= cfg.irradiance_threshold_wm2 and (p.expected_kwh or 0) > 0


# --------------------------------------------------------------------------- rules


def rule_plant_zero_output(hours: list[HourPoint], cfg: RuleConfig) -> list[Finding]:
    flags: list[bool | None] = [
        None if not _daylight(p, cfg) or p.actual_kwh is None else
        (not p.excluded and p.actual_kwh <= cfg.zero_output_ratio * p.expected_kwh)
        for p in hours
    ]
    out = []
    for s, e in _runs(flags):
        n = _true_count(flags, s, e)
        if n < cfg.min_run_hours:
            continue
        seg = [p for p, f in zip(hours[s:e + 1], flags[s:e + 1], strict=True) if f]
        loss = sum(p.expected_kwh - p.actual_kwh for p in seg)
        out.append(Finding(
            "PLANT_ZERO_OUTPUT", "1.0", "downtime", "critical" if n >= 6 else "high", "high",
            f"Plant produced ~no energy for {n} h despite irradiance",
            f"Metered output was ≤{cfg.zero_output_ratio:.0%} of expected for {n} consecutive hours while plane-of-array "
            f"irradiance averaged {statistics.mean(p.poa_wm2 for p in seg):.0f} W/m². No curtailment, grid outage or "
            f"planned maintenance event covers this period. {DISCLAIMER}",
            {"hours": n, "mean_poa_wm2": round(statistics.mean(p.poa_wm2 for p in seg), 1),
             "expected_kwh": round(sum(p.expected_kwh for p in seg), 2), "actual_kwh": round(sum(p.actual_kwh for p in seg), 2),
             "threshold_ratio": cfg.zero_output_ratio, "irradiance_threshold_wm2": cfg.irradiance_threshold_wm2},
            ["Check grid connection, main breaker and protection relay trips",
             "Review inverter and SCADA alarm logs for the window",
             "Confirm meter communication (zero may be a data fault rather than a production fault)",
             "Record an outage event if the cause is confirmed"],
            seg[0].ts, seg[-1].ts, loss))
    return out


def rule_inverter_offline(hours: list[HourPoint], invs: list[InverterInfo], cfg: RuleConfig) -> list[Finding]:
    if len(invs) < 2:
        return []
    total_kwp = sum(i.dc_kwp for i in invs)
    out = []
    for inv in invs:
        share = inv.dc_kwp / total_kwp
        flags: list[bool | None] = []
        for p in hours:
            v = p.inverters.get(inv.id)
            if not _daylight(p, cfg) or p.excluded or v is None:
                flags.append(None)
                continue
            exp_inv = p.expected_kwh * share
            peers = [(p.inverters.get(o.id), o.dc_kwp / total_kwp) for o in invs if o.id != inv.id]
            peer_ok = [pv for pv, sh in peers if pv is not None and pv >= cfg.inverter_peer_ratio * p.expected_kwh * sh]
            flags.append(v <= cfg.zero_output_ratio * exp_inv and len(peer_ok) >= 1)
        for s, e in _runs(flags):
            n = _true_count(flags, s, e)
            if n < cfg.min_run_hours:
                continue
            seg = [p for p, f in zip(hours[s:e + 1], flags[s:e + 1], strict=True) if f]
            loss = sum(p.expected_kwh * share - (p.inverters.get(inv.id) or 0) for p in seg)
            out.append(Finding(
                "INVERTER_OFFLINE", "1.0", "inverter", "high" if n >= 4 else "medium", "high",
                f"{inv.name} offline for {n} daylight h while peers produced",
                f"{inv.name} reported ≤{cfg.zero_output_ratio:.0%} of its expected share for {n} consecutive daylight hours "
                f"while at least one peer inverter produced normally — pointing to an inverter-level rather than "
                f"plant-level issue. {DISCLAIMER}",
                {"daylight_hours": n, "span_days": round((seg[-1].ts - seg[0].ts).total_seconds() / 86400, 2),
                 "inverter_dc_kwp": inv.dc_kwp, "expected_share": round(share, 4),
                 "expected_inverter_kwh": round(sum(p.expected_kwh * share for p in seg), 2)},
                ["Check inverter fault codes and event log", "Inspect AC/DC isolators and fuses",
                 "Check warranty status before ordering parts", "Create a corrective work order"],
                seg[0].ts, seg[-1].ts, loss, equipment_id=inv.id))
    return out


def rule_inverter_underperformance(hours: list[HourPoint], invs: list[InverterInfo], cfg: RuleConfig) -> list[Finding]:
    """Weekly normalised yield of each inverter vs the median of its peers. Consecutive weeks below the
    threshold are merged into a single finding with a weekly breakdown."""
    if len(invs) < 3:
        return []
    weeks: dict[datetime, list[HourPoint]] = defaultdict(list)
    for p in hours:
        wk = (p.ts - timedelta(days=p.ts.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weeks[wk].append(p)
    hits: dict[str, list[dict]] = defaultdict(list)
    for wk, pts in sorted(weeks.items()):
        valid = [p for p in pts if _daylight(p, cfg) and not p.excluded
                 and all(p.inverters.get(i.id) is not None for i in invs)
                 and all((p.inverters.get(i.id) or 0) > 0 for i in invs)]  # offline hours handled by INVERTER_OFFLINE
        if len(valid) < 20:
            continue
        yields = {i.id: sum(p.inverters[i.id] for p in valid) / i.dc_kwp for i in invs}
        for inv in invs:
            med = statistics.median([y for k, y in yields.items() if k != inv.id])
            if med <= 0:
                continue
            deficit = 1 - yields[inv.id] / med
            if deficit >= cfg.underperf_deficit:
                hits[inv.id].append({"week": wk, "start": valid[0].ts, "end": valid[-1].ts, "yield": yields[inv.id],
                                     "median": med, "deficit": deficit, "loss": (med - yields[inv.id]) * inv.dc_kwp,
                                     "hours": len(valid)})
    out = []
    by_id = {i.id: i for i in invs}
    for inv_id, hs in hits.items():
        inv = by_id[inv_id]
        groups: list[list[dict]] = []
        for h in hs:
            if groups and h["week"] - groups[-1][-1]["week"] <= timedelta(days=7):
                groups[-1].append(h)
            else:
                groups.append([h])
        for g in groups:
            loss = sum(h["loss"] for h in g)
            mean_def = statistics.mean(h["deficit"] for h in g)
            y, m = sum(h["yield"] for h in g), sum(h["median"] for h in g)
            out.append(Finding(
                "INVERTER_UNDERPERFORMANCE", "1.1", "inverter", "high" if mean_def >= 0.25 else "medium",
                "high" if len(g) >= 3 else "medium",
                f"{inv.name} {mean_def:.0%} below peer median for {len(g)} week(s)",
                f"Normalised yield of {inv.name} was {y:.1f} kWh/kWp vs a peer median of {m:.1f} kWh/kWp over "
                f"{sum(h['hours'] for h in g)} comparable daylight hours in {len(g)} consecutive week(s). Possible causes "
                f"include failed strings, blown fuses, shading, MPPT issues or a configuration error. {DISCLAIMER}",
                {"mean_deficit_pct": round(mean_def * 100, 2), "weeks": len(g), "peers": len(invs) - 1,
                 "weekly": [{"week": h["week"].date().isoformat(), "inverter_kwh_per_kwp": round(h["yield"], 3),
                             "peer_median_kwh_per_kwp": round(h["median"], 3), "deficit_pct": round(h["deficit"] * 100, 2)}
                            for h in g]},
                ["Run string current / IV-curve checks on this inverter", "Check string fuses and connectors",
                 "Inspect for new shading or soiling local to this block", "Review MPPT and derating settings"],
                g[0]["start"], g[-1]["end"], loss, equipment_id=inv.id))
    return out


def _daily_pi(hours: list[HourPoint], cfg: RuleConfig, explained: set[datetime] = frozenset()) -> list[tuple[datetime, float, float, float]]:
    """Daily performance index over operating daylight hours, skipping hours already explained by
    another finding (e.g. an offline inverter) so the same loss is not flagged twice."""
    days: dict[datetime, list[HourPoint]] = defaultdict(list)
    for p in hours:
        if _daylight(p, cfg) and not p.excluded and p.actual_kwh is not None and p.ts not in explained \
                and p.actual_kwh > cfg.zero_output_ratio * p.expected_kwh:
            days[p.ts.replace(hour=0, minute=0, second=0, microsecond=0)].append(p)
    out = []
    for d, pts in sorted(days.items()):
        if len(pts) < 4:
            continue
        a = sum(p.actual_kwh for p in pts)
        e = sum(p.expected_kwh for p in pts)
        out.append((d, a / e, e, a))
    return out


def rule_persistent_low_pi(hours: list[HourPoint], cfg: RuleConfig, explained: set[datetime] = frozenset()) -> list[Finding]:
    daily = _daily_pi(hours, cfg, explained)
    out = []
    i = 0
    while i + cfg.low_pi_window_days <= len(daily):
        win = daily[i:i + cfg.low_pi_window_days]
        low = [d for d in win if d[1] < cfg.low_pi_threshold]
        if len(low) >= cfg.low_pi_days_required:
            loss = sum(d[2] - d[3] for d in low)
            mean_pi = statistics.mean(d[1] for d in low)
            out.append(Finding(
                "PERSISTENT_LOW_PI", "1.0", "performance", "high" if mean_pi < 0.8 else "medium", "medium",
                f"Performance index below {cfg.low_pi_threshold:.0%} on {len(low)} of {len(win)} days",
                f"Daily performance index (actual ÷ temperature-corrected expected, operating hours only) averaged "
                f"{mean_pi:.1%} on {len(low)} days. Persistent partial underperformance is consistent with soiling, "
                f"string failures, degradation or an irradiance-sensor calibration drift. {DISCLAIMER}",
                {"days_low": len(low), "window_days": len(win), "mean_pi_low_days": round(mean_pi, 4),
                 "threshold": cfg.low_pi_threshold, "daily_pi": {d[0].date().isoformat(): round(d[1], 4) for d in win}},
                ["Visual inspection for soiling and compare with a cleaned reference string",
                 "Verify pyranometer cleanliness and calibration date", "Check string-level currents for failures"],
                win[0][0], win[-1][0] + timedelta(hours=23), loss))
            i += cfg.low_pi_window_days
        else:
            i += 1
    return out


def rule_sensor_flatline(hours: list[HourPoint], cfg: RuleConfig) -> list[Finding]:
    out = []
    flags = [False] * len(hours)
    for i in range(1, len(hours)):
        a, b = hours[i - 1].poa_wm2, hours[i].poa_wm2
        if a is not None and b is not None and a > 10 and abs(a - b) < 1e-6:
            flags[i - 1] = flags[i] = True
    for s, e in _runs(flags):
        if e - s + 1 >= cfg.flatline_hours:
            out.append(Finding(
                "IRRADIANCE_SENSOR_FLATLINE", "1.0", "sensor", "medium", "high",
                f"Irradiance sensor stuck at {hours[s].poa_wm2:.0f} W/m² for {e - s + 1} h",
                "Plane-of-array irradiance reported an identical value for consecutive hours, which is physically "
                "implausible in daylight. Expected-generation and loss estimates for this window are unreliable. " + DISCLAIMER,
                {"value_wm2": hours[s].poa_wm2, "hours": e - s + 1},
                ["Inspect pyranometer and data-logger channel", "Exclude or backfill the window from satellite data"],
                hours[s].ts, hours[e].ts, 0.0))
    # Sensor reads ~0 while the plant produces substantially.
    dark = [(p.poa_wm2 is not None and p.poa_wm2 < 5 and p.actual_kwh is not None and p.expected_kwh is not None
             and p.actual_kwh > 0 and 8 <= p.ts.hour <= 16) for p in hours]
    for s, e in _runs(dark):
        if e - s + 1 >= cfg.min_run_hours:
            out.append(Finding(
                "IRRADIANCE_SENSOR_DARK", "1.0", "sensor", "medium", "medium",
                f"Irradiance ~0 W/m² while plant produced for {e - s + 1} h",
                "The irradiance sensor reported near-zero values while metered output was positive. " + DISCLAIMER,
                {"hours": e - s + 1, "actual_kwh": round(sum(p.actual_kwh for p in hours[s:e + 1]), 2)},
                ["Check pyranometer cabling and logger channel"], hours[s].ts, hours[e].ts, 0.0))
    return out


def rule_data_gap(hours: list[HourPoint], cfg: RuleConfig) -> list[Finding]:
    flags = [p.actual_kwh is None and (p.expected_kwh or 0) > 0 for p in hours]
    out = []
    for s, e in _runs(flags):
        n = e - s + 1
        if n >= cfg.gap_hours:
            exp = sum(p.expected_kwh for p in hours[s:e + 1])
            out.append(Finding(
                "DATA_GAP", "1.0", "data_quality", "medium" if n >= 12 else "low", "high",
                f"Missing production data for {n} daylight hours",
                "No metered production was received for this window. Losses during gaps are unknown and are excluded "
                "from financial calculations rather than counted as zero.",
                {"hours": n, "expected_kwh_unverifiable": round(exp, 2)},
                ["Check data-logger and communication link", "Backfill from inverter memory or meter if available"],
                hours[s].ts, hours[e].ts, 0.0))
    return out


def _trend(daily: list[tuple[datetime, float, float, float]]) -> tuple[float, float, float]:
    xs = [(d[0] - daily[0][0]).days for d in daily]
    ys = [d[1] for d in daily]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return slope, intercept, (1 - ss_res / ss_tot if ss_tot > 0 else 0.0)


def rule_declining_trend(hours: list[HourPoint], cfg: RuleConfig, explained: set[datetime] = frozenset()) -> list[Finding]:
    """Least-squares slope of daily PI over sliding windows of ``trend_min_days`` (step 7 days).
    Overlapping qualifying windows are merged into one finding."""
    daily = _daily_pi(hours, cfg, explained)
    w = cfg.trend_min_days
    hits: list[tuple[int, int, float, float]] = []
    for i in range(0, max(0, len(daily) - w + 1), 7):
        slope, _, r2 = _trend(daily[i:i + w])
        if slope <= cfg.trend_slope_per_day and r2 >= cfg.trend_min_r2:
            if hits and i <= hits[-1][1]:
                hits[-1] = (hits[-1][0], i + w - 1, min(hits[-1][2], slope), max(hits[-1][3], r2))
            else:
                hits.append((i, i + w - 1, slope, r2))
    out = []
    for a, b, _, _ in hits:
        seg = daily[a:b + 1]
        slope, intercept, r2 = _trend(seg)
        loss = sum(max(0.0, intercept - d[1]) * d[2] for d in seg)
        out.append(Finding(
            "DECLINING_PI_TREND", "1.1", "soiling", "medium" if slope > -0.006 else "high", "low",
            f"Performance index declining {abs(slope) * 100:.2f} %-points/day over {len(seg)} days",
            f"Daily performance index fell steadily from {seg[0][0].date()} to {seg[-1][0].date()} "
            f"(slope {slope * 100:.2f} %-pt/day, R²={r2:.2f}). A gradual decline that resets after rain or cleaning is "
            f"typical of soiling; a decline that does not reset may indicate degradation or sensor drift. {DISCLAIMER}",
            {"slope_per_day": round(slope, 5), "r2": round(r2, 3), "days": len(seg), "pi_start": round(intercept, 4),
             "pi_end": round(seg[-1][1], 4)},
            ["Inspect module surfaces for dust / bird droppings", "Clean a reference row and compare yield for 3 days",
             "Check rainfall history for the period"],
            seg[0][0], seg[-1][0] + timedelta(hours=23), loss))
    return out


def run_all(hours: list[HourPoint], invs: list[InverterInfo], cfg: RuleConfig = RuleConfig()) -> list[Finding]:
    hours = sorted(hours, key=lambda p: p.ts)
    findings: list[Finding] = []
    findings += rule_plant_zero_output(hours, cfg)
    offline = rule_inverter_offline(hours, invs, cfg)
    findings += offline
    sensor = rule_sensor_flatline(hours, cfg)
    findings += sensor
    # Hours already explained by an offline inverter or unreliable irradiance are excluded from plant-level PI rules.
    explained = {p.ts for f in offline + sensor for p in hours if f.window_start <= p.ts <= f.window_end}
    findings += rule_inverter_underperformance(hours, invs, cfg)
    findings += rule_persistent_low_pi(hours, cfg, explained)
    findings += rule_data_gap(hours, cfg)
    findings += rule_declining_trend(hours, cfg, explained)
    return findings


def priority_score(severity: str, financial_impact: float, window_end: datetime, data_end: datetime) -> tuple[float, float]:
    """Return (urgency 0..1, priority 0..100). Urgency is 1 for ongoing issues and decays over 30 days."""
    age_days = max(0.0, (data_end - window_end).total_seconds() / 86400)
    urgency = 1.0 if age_days <= 1 else max(0.0, 1 - age_days / 30)
    impact = min(1.0, math.log10(1 + max(0.0, financial_impact)) / 5)  # 100k currency units -> 1.0
    score = 40 * SEVERITY_WEIGHT[severity] / 4 + 35 * impact + 25 * urgency
    return round(urgency, 3), round(score, 1)
