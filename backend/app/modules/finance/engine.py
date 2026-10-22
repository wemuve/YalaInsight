"""Financial loss & recovery engine (pure, deterministic, Decimal arithmetic).

Key principles (see docs/CALCULATIONS.md):

* Each kWh of shortfall is attributed to exactly one cause (priority waterfall), so overlapping
  events, maintenance windows and curtailment are never double-counted.
* Intervals without both an actual and an expected value are excluded and reported as data gaps.
* Lost *production* (kWh) is distinguished from lost *revenue* (money): curtailed or grid-unavailable
  energy that the contract compensates produces a production loss but no revenue loss.
* Historical losses are sunk. "Recoverable" value is the *future* value of avoiding a loss that is
  expected to persist, after repair cost, incremental cost and the delay until the fix is effective.
* Every output figure is labelled with its basis: measured, modelled, simulated or verified.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

ENGINE_VERSION = "1.0.0"

KWH = Decimal("0.001")
MONEY = Decimal("0.01")
RATIO = Decimal("0.0001")
ZERO = Decimal(0)


def D(x: float | int | str | Decimal | None) -> Decimal:
    if x is None:
        return ZERO
    return x if isinstance(x, Decimal) else Decimal(str(x))


def q(x: Decimal, unit: Decimal) -> Decimal:
    return x.quantize(unit, rounding=ROUND_HALF_UP)


class Basis(StrEnum):
    MEASURED = "measured"    # metered data
    MODELLED = "modelled"    # derived from a physical / statistical model (e.g. expected generation)
    SIMULATED = "simulated"  # depends on user-supplied scenario assumptions
    VERIFIED = "verified"    # confirmed by inspection, post-repair measurement or third party


class LossCategory(StrEnum):
    CURTAILMENT = "curtailment"
    GRID_OUTAGE = "grid_outage"
    PLANNED_MAINTENANCE = "planned_maintenance"
    PLANT_DOWNTIME = "plant_downtime"
    INVERTER_FAILURE = "inverter_failure"
    SOILING = "soiling"
    DEGRADATION = "excess_degradation"
    UNEXPLAINED = "unexplained"


# Priority order of the attribution waterfall for event-driven categories.
EVENT_PRIORITY = [LossCategory.CURTAILMENT, LossCategory.GRID_OUTAGE, LossCategory.PLANNED_MAINTENANCE,
                  LossCategory.PLANT_DOWNTIME]
# Operational-event categories (as stored in operational_events.category) -> loss category.
EVENT_TO_LOSS = {
    "curtailment": LossCategory.CURTAILMENT,
    "grid_outage": LossCategory.GRID_OUTAGE,
    "planned_maintenance": LossCategory.PLANNED_MAINTENANCE,
    "outage": LossCategory.PLANT_DOWNTIME,
    "plant_downtime": LossCategory.PLANT_DOWNTIME,
}
EXTERNAL_CATEGORIES = {LossCategory.CURTAILMENT, LossCategory.GRID_OUTAGE}  # not caused by plant condition
ADDRESSABLE_CATEGORIES = {LossCategory.PLANT_DOWNTIME, LossCategory.INVERTER_FAILURE, LossCategory.SOILING,
                          LossCategory.DEGRADATION, LossCategory.UNEXPLAINED}


@dataclass(frozen=True)
class Figure:
    value: Decimal
    unit: str
    basis: Basis
    formula: str = ""
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"value": float(self.value), "unit": self.unit, "basis": self.basis.value, "formula": self.formula,
                "inputs": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in self.inputs.items()}}


# --------------------------------------------------------------------------- interval inputs


@dataclass
class HourInput:
    ts: datetime
    expected_kwh: float | None
    actual_kwh: float | None
    events: frozenset[str] = frozenset()     # plant-wide event categories active in this interval
    inverter_fault_expected_kwh: float = 0.0  # expected output of inverters with an active fault/outage
    export_cap_kwh: float | None = None       # contractual/grid export limit for the interval


@dataclass
class Attribution:
    by_category: dict[LossCategory, Decimal]
    total_shortfall_kwh: Decimal
    expected_kwh: Decimal
    actual_kwh: Decimal
    overperformance_kwh: Decimal
    intervals_total: int
    intervals_used: int
    intervals_excluded: int
    hours_by_category: dict[LossCategory, int]
    expected_scale: Decimal

    @property
    def data_coverage(self) -> Decimal:
        return q(D(self.intervals_used) / D(self.intervals_total), RATIO) if self.intervals_total else ZERO


@dataclass(frozen=True)
class ModelledLossShares:
    """User/analyst estimates applied to the residual (non-event) shortfall, as % of expected energy."""
    soiling_pct: float = 0.0
    excess_degradation_pct: float = 0.0


def attribute_losses(hours: Iterable[HourInput], shares: ModelledLossShares = ModelledLossShares(),
                     expected_scale: float | Decimal = 1) -> Attribution:
    """Attribute energy shortfall to causes without double counting.

    1. Event intervals (curtailment > grid outage > planned maintenance > plant downtime): the whole
       positive shortfall of the interval goes to the highest-priority active event.
    2. Inverter-fault intervals: shortfall up to the faulted inverters' expected output goes to
       ``inverter_failure``; any remainder joins the residual pool.
    3. Residual pool (all other intervals, net of over-performance) is split into soiling and excess
       degradation up to the analyst's estimated shares, with the remainder ``unexplained``.

    ``expected_scale`` scales the expected baseline (e.g. 0.95 / 1.05) to propagate model uncertainty.
    """
    scale = D(expected_scale)
    cats = {c: ZERO for c in LossCategory}
    hours_by = {c: 0 for c in LossCategory}
    total = used = 0
    exp_sum = act_sum = ZERO
    pool = ZERO
    pool_expected = ZERO
    over = ZERO
    for h in hours:
        total += 1
        if h.expected_kwh is None or h.actual_kwh is None:
            continue
        used += 1
        expected = D(h.expected_kwh) * scale
        if h.export_cap_kwh is not None:
            expected = min(expected, D(h.export_cap_kwh))
        actual = D(h.actual_kwh)
        exp_sum += expected
        act_sum += actual
        gap = expected - actual
        active = {EVENT_TO_LOSS[e] for e in h.events if e in EVENT_TO_LOSS}
        event = next((c for c in EVENT_PRIORITY if c in active), None)
        if event is not None:
            if gap > 0:
                cats[event] += gap
                hours_by[event] += 1
            continue
        if h.inverter_fault_expected_kwh > 0 and gap > 0:
            inv = min(gap, D(h.inverter_fault_expected_kwh) * scale)
            cats[LossCategory.INVERTER_FAILURE] += inv
            hours_by[LossCategory.INVERTER_FAILURE] += 1
            gap -= inv
            expected -= inv  # faulted capacity is excluded from the residual pool's baseline
        pool += gap
        pool_expected += expected
    if pool > 0:
        remaining = pool
        for cat, pct in ((LossCategory.SOILING, shares.soiling_pct), (LossCategory.DEGRADATION, shares.excess_degradation_pct)):
            take = min(remaining, pool_expected * D(pct) / 100)
            cats[cat] += take
            remaining -= take
        cats[LossCategory.UNEXPLAINED] += remaining
    else:
        over = -pool
    total_short = sum(cats.values(), ZERO)
    return Attribution(
        by_category={c: q(v, KWH) for c, v in cats.items()},
        total_shortfall_kwh=q(total_short, KWH),
        expected_kwh=q(exp_sum, KWH), actual_kwh=q(act_sum, KWH), overperformance_kwh=q(over, KWH),
        intervals_total=total, intervals_used=used, intervals_excluded=total - used,
        hours_by_category=hours_by, expected_scale=scale,
    )


def energy_shortfall(expected_kwh: float | Decimal, actual_kwh: float | Decimal, excluded_kwh: float | Decimal = 0) -> Decimal:
    """Energy shortfall = expected − actual − valid exclusions (never negative)."""
    return q(max(ZERO, D(expected_kwh) - D(actual_kwh) - D(excluded_kwh)), KWH)


def merge_intervals(intervals: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping/adjacent periods so overlapping events are not double-counted in durations."""
    out: list[list[datetime]] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def total_duration_hours(intervals: Sequence[tuple[datetime, datetime]]) -> Decimal:
    return q(sum((D((e - s).total_seconds()) / 3600 for s, e in merge_intervals(intervals)), ZERO), Decimal("0.01"))


# --------------------------------------------------------------------------- valuation


@dataclass(frozen=True)
class Tariff:
    """Energy value. ``kind``: ppa | feed_in_tariff | self_consumption | merchant."""
    kind: str
    price_per_kwh: Decimal
    currency: str
    valid_from: date
    valid_to: date | None = None
    escalation_pct_per_year: Decimal = ZERO
    curtailment_compensated: bool = False

    def price_on(self, d: date) -> Decimal:
        years = max(0, (d - self.valid_from).days) // 365
        return D(self.price_per_kwh) * (1 + D(self.escalation_pct_per_year) / 100) ** years

    def covers(self, d: date) -> bool:
        return self.valid_from <= d and (self.valid_to is None or d <= self.valid_to)


TARIFF_NOTES = {
    "ppa": "Valued at the contracted PPA energy rate. Capacity / availability payments are not included.",
    "feed_in_tariff": "Valued at the feed-in tariff for exported energy.",
    "self_consumption": "Valued at the avoided retail import cost; only valid for energy consumed on site.",
    "merchant": "Valued at the configured market price; realised prices vary hourly.",
}


def effective_price(tariffs: Sequence[Tariff], on: date) -> Tariff:
    for t in sorted(tariffs, key=lambda t: t.valid_from, reverse=True):
        if t.covers(on):
            return t
    raise ValueError(f"No energy contract/tariff covers {on.isoformat()}")


def value_losses(att: Attribution, tariff: Tariff, price_date: date) -> dict:
    """Translate attributed kWh into gross lost energy value and lost revenue per category."""
    price = tariff.price_on(price_date)
    rows = {}
    gross = revenue = ZERO
    for cat, kwh in att.by_category.items():
        g = kwh * price
        compensated = cat in EXTERNAL_CATEGORIES and tariff.curtailment_compensated
        r = ZERO if compensated else g
        gross += g
        revenue += r
        rows[cat.value] = {
            "lost_production_kwh": Figure(kwh, "kWh", Basis.MODELLED, "attributed expected − actual",
                                          {"intervals": att.hours_by_category[cat]}).to_dict(),
            "gross_value": Figure(q(g, MONEY), tariff.currency, Basis.MODELLED, "lost_production_kwh × price",
                                  {"price_per_kwh": price}).to_dict(),
            "lost_revenue": Figure(q(r, MONEY), tariff.currency, Basis.MODELLED,
                                   "0 (compensated under contract)" if compensated else "gross_value",
                                   {"contract_compensates": compensated}).to_dict(),
            "addressable_by_maintenance": cat in ADDRESSABLE_CATEGORIES,
        }
    return {"categories": rows, "gross_value": q(gross, MONEY), "lost_revenue": q(revenue, MONEY), "price_per_kwh": price,
            "currency": tariff.currency, "tariff_kind": tariff.kind, "tariff_note": TARIFF_NOTES.get(tariff.kind, "")}


# --------------------------------------------------------------------------- currency


@dataclass(frozen=True)
class FxRate:
    base: str
    quote: str
    rate: Decimal
    rate_date: date
    source: str


def convert(amount: Decimal, from_ccy: str, to_ccy: str, rates: Sequence[FxRate], on: date) -> tuple[Decimal, FxRate | None]:
    """Convert with the most recent explicit rate dated on/before ``on``. Never guesses a rate."""
    if from_ccy == to_ccy:
        return amount, None
    best: tuple[FxRate, bool] | None = None
    for r in rates:
        if r.rate_date > on:
            continue
        direct = r.base == from_ccy and r.quote == to_ccy
        inverse = r.base == to_ccy and r.quote == from_ccy
        if (direct or inverse) and (best is None or r.rate_date > best[0].rate_date):
            best = (r, direct)
    if best is None:
        raise ValueError(f"No exchange rate {from_ccy}->{to_ccy} dated on or before {on.isoformat()}")
    r, direct = best
    return (q(amount * r.rate, MONEY) if direct else q(amount / r.rate, MONEY)), r


# --------------------------------------------------------------------------- recovery simulation


@dataclass(frozen=True)
class RecoveryInput:
    annual_addressable_loss_kwh: Decimal   # expected future loss per year if nothing is done
    recovery_rate: Decimal                 # 0..1 share of that loss the intervention removes
    energy_price: Decimal                  # value per kWh of recovered energy (revenue basis)
    repair_cost: Decimal                   # one-off cost
    incremental_annual_cost: Decimal = ZERO  # e.g. extra cleaning cycles, monitoring contract
    days_to_effect: int = 0                # downtime / lead time before recovery starts
    horizon_years: int = 5
    discount_rate: Decimal = Decimal("0.08")
    annual_degradation_pct: Decimal = Decimal("0.5")
    price_escalation_pct: Decimal = ZERO


def simulate_recovery(inp: RecoveryInput) -> dict:
    if not (ZERO <= D(inp.recovery_rate) <= 1):
        raise ValueError("recovery_rate must be between 0 and 1")
    if inp.horizon_years < 1:
        raise ValueError("horizon_years must be >= 1")
    rec_kwh = D(inp.annual_addressable_loss_kwh) * D(inp.recovery_rate)
    years = []
    cumulative = -D(inp.repair_cost)
    npv = -D(inp.repair_cost)
    payback: Decimal | None = None
    undiscounted = ZERO
    for y in range(1, inp.horizon_years + 1):
        active = D(1)
        if y == 1 and inp.days_to_effect:
            active = max(ZERO, D(365 - min(inp.days_to_effect, 365)) / 365)
        kwh = rec_kwh * (1 - D(inp.annual_degradation_pct) / 100) ** (y - 1) * active
        price = D(inp.energy_price) * (1 + D(inp.price_escalation_pct) / 100) ** (y - 1)
        value = kwh * price
        net = value - D(inp.incremental_annual_cost)
        prev = cumulative
        cumulative += net
        undiscounted += net
        npv += net / (1 + D(inp.discount_rate)) ** y
        if payback is None and cumulative >= 0 and net > 0:
            payback = D(y - 1) + (-prev / net if prev < 0 else ZERO)
        years.append({"year": y, "recovered_kwh": float(q(kwh, KWH)), "value": float(q(value, MONEY)),
                      "net_cash_flow": float(q(net, MONEY)), "cumulative": float(q(cumulative, MONEY))})
    net_benefit = undiscounted - D(inp.repair_cost)
    total_cost = D(inp.repair_cost) + D(inp.incremental_annual_cost) * inp.horizon_years
    roi = (net_benefit / total_cost) if total_cost > 0 else None
    first_year_value = D(years[0]["value"])
    return {
        "annual_recovered_kwh": q(rec_kwh, KWH),
        "annual_recovered_value": q(rec_kwh * D(inp.energy_price), MONEY),
        "first_year_value": q(first_year_value, MONEY),
        "net_benefit": q(net_benefit, MONEY),
        "npv": q(npv, MONEY),
        "roi": q(roi, RATIO) if roi is not None else None,
        "payback_years": q(payback, Decimal("0.01")) if payback is not None else None,
        "cash_flows": years,
    }


@dataclass(frozen=True)
class ScenarioAssumptions:
    name: str
    expected_scale: Decimal       # baseline uncertainty multiplier on expected energy
    recovery_rate: Decimal
    price_multiplier: Decimal
    repair_cost_multiplier: Decimal
    days_to_effect: int


def default_scenarios(uncertainty_pct: float, recovery_rate: float, days_to_effect: int) -> list[ScenarioAssumptions]:
    u = D(uncertainty_pct) / 100
    r = D(recovery_rate)
    return [
        ScenarioAssumptions("low", 1 - u, max(ZERO, r - Decimal("0.2")), Decimal("0.9"), Decimal("1.25"), days_to_effect * 2),
        ScenarioAssumptions("base", D(1), r, D(1), D(1), days_to_effect),
        ScenarioAssumptions("high", 1 + u, min(D(1), r + Decimal("0.1")), Decimal("1.1"), Decimal("0.85"), max(0, days_to_effect // 2)),
    ]


def sensitivity(base: RecoveryInput, variation: Decimal = Decimal("0.2")) -> list[dict]:
    """One-at-a-time ±variation on key drivers; returns NPV swing per driver (tornado data)."""
    base_npv = simulate_recovery(base).get("npv")
    out = []
    drivers = {
        "energy_price": lambda v: {"energy_price": D(base.energy_price) * v},
        "recovery_rate": lambda v: {"recovery_rate": min(D(1), D(base.recovery_rate) * v)},
        "repair_cost": lambda v: {"repair_cost": D(base.repair_cost) * v},
        "annual_addressable_loss_kwh": lambda v: {"annual_addressable_loss_kwh": D(base.annual_addressable_loss_kwh) * v},
        "days_to_effect": lambda v: {"days_to_effect": int(round(base.days_to_effect * float(v)))},
    }
    if base.days_to_effect == 0:
        del drivers["days_to_effect"]
    for name, fn in drivers.items():
        lo = simulate_recovery(_replace(base, **fn(1 - variation)))["npv"]
        hi = simulate_recovery(_replace(base, **fn(1 + variation)))["npv"]
        out.append({"driver": name, "npv_at_minus": float(lo), "npv_at_plus": float(hi), "base_npv": float(base_npv),
                    "swing": float(abs(hi - lo))})
    return sorted(out, key=lambda r: -r["swing"])


def _replace(inp: RecoveryInput, **kw) -> RecoveryInput:
    d = inp.__dict__.copy()
    d.update(kw)
    return RecoveryInput(**d)


def annualise(kwh: Decimal, period_days: Decimal | int | float) -> Decimal:
    """Scale a loss observed over ``period_days`` to a year, assuming it persists at the same rate.
    Note: ignores seasonality — the period should ideally span a full year."""
    if D(period_days) <= 0:
        raise ValueError("period_days must be positive")
    return q(D(kwh) * 365 / D(period_days), KWH)
