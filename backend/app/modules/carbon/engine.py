"""Avoided-emissions estimate (pure).

    avoided_tCO2e = metered_generation_kWh × EF[kgCO2e/kWh] / 1000

This is an *estimate* of emissions displaced from the grid using a configurable
grid emission factor. It is not an independently verified emission reduction
and does not constitute a carbon credit; verified reductions are recorded
separately and never mixed with estimates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..finance.engine import D, q

METHOD_NOTE = ("Estimated avoided emissions = metered net generation (kWh) × grid emission factor (kgCO2e/kWh) ÷ 1000. "
               "Uses actual metered generation only (not expected). Excludes lifecycle emissions of the plant, "
               "transmission losses and marginal-vs-average grid effects unless the chosen factor reflects them.")


@dataclass(frozen=True)
class FactorRef:
    name: str
    kg_co2e_per_kwh: Decimal
    method: str
    source: str
    year: int | None
    is_sample: bool


def avoided_emissions(generation_kwh: float | Decimal, factor: FactorRef, period_start: date, period_end: date) -> dict:
    t = q(D(generation_kwh) * D(factor.kg_co2e_per_kwh) / 1000, Decimal("0.001"))
    warnings = []
    if factor.is_sample:
        warnings.append("Emission factor is an illustrative sample value — configure an authoritative factor before reporting.")
    return {
        "estimated_avoided_tco2e": t,
        "generation_kwh": q(D(generation_kwh), Decimal("0.001")),
        "basis": "estimated",
        "verified": False,
        "factor": {"name": factor.name, "kg_co2e_per_kwh": float(factor.kg_co2e_per_kwh), "method": factor.method,
                   "source": factor.source, "year": factor.year, "is_sample": factor.is_sample},
        "units": {"generation": "kWh", "factor": "kgCO2e/kWh", "result": "tCO2e"},
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "methodology": METHOD_NOTE,
        "warnings": warnings,
    }
