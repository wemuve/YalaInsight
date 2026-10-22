from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.modules.finance.engine import (
    Basis,
    FxRate,
    HourInput,
    LossCategory,
    ModelledLossShares,
    RecoveryInput,
    Tariff,
    annualise,
    attribute_losses,
    convert,
    default_scenarios,
    effective_price,
    energy_shortfall,
    merge_intervals,
    sensitivity,
    simulate_recovery,
    total_duration_hours,
    value_losses,
)

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)


def h(i, exp, act, events=(), inv=0.0, cap=None):
    return HourInput(T0 + timedelta(hours=i), exp, act, frozenset(events), inv, cap)


def test_energy_shortfall_basic_and_clamped():
    assert energy_shortfall(1000, 900) == Decimal("100.000")
    assert energy_shortfall(1000, 900, excluded_kwh=30) == Decimal("70.000")
    assert energy_shortfall(1000, 1100) == Decimal("0.000")


def test_attribution_sums_to_total_and_no_double_counting():
    hours = [
        h(0, 100, 0, events={"curtailment", "outage"}),  # overlap: curtailment wins, counted once
        h(1, 100, 0, events={"outage"}),
        h(2, 100, 75, inv=25),                            # inverter offline, exact share
        h(3, 100, 60, inv=25),                            # 25 inverter, 15 to residual pool
        h(4, 100, 95),                                    # 5 residual
    ]
    att = attribute_losses(hours, ModelledLossShares(soiling_pct=2.0))
    c = att.by_category
    assert c[LossCategory.CURTAILMENT] == Decimal("100.000")
    assert c[LossCategory.PLANT_DOWNTIME] == Decimal("100.000")
    assert c[LossCategory.INVERTER_FAILURE] == Decimal("50.000")
    # residual pool = 15 + 5 = 20 ; pool expected = 75 + 75 + 100 = 250 -> soiling 2 % = 5
    assert c[LossCategory.SOILING] == Decimal("5.000")
    assert c[LossCategory.UNEXPLAINED] == Decimal("15.000")
    assert att.total_shortfall_kwh == sum(c.values())
    assert att.total_shortfall_kwh == energy_shortfall(att.expected_kwh, att.actual_kwh)


def test_modelled_shares_capped_by_residual():
    att = attribute_losses([h(0, 100, 99)], ModelledLossShares(soiling_pct=5, excess_degradation_pct=5))
    assert att.by_category[LossCategory.SOILING] == Decimal("1.000")
    assert att.by_category[LossCategory.DEGRADATION] == Decimal("0.000")
    assert att.by_category[LossCategory.UNEXPLAINED] == Decimal("0.000")


def test_overperformance_offsets_residual_not_events():
    att = attribute_losses([h(0, 100, 0, events={"outage"}), h(1, 100, 130)])
    assert att.by_category[LossCategory.PLANT_DOWNTIME] == Decimal("100.000")
    assert att.by_category[LossCategory.UNEXPLAINED] == Decimal("0.000")
    assert att.overperformance_kwh == Decimal("30.000")


def test_missing_data_is_excluded_not_zero():
    att = attribute_losses([h(0, 100, None), h(1, None, 50), h(2, 100, 90)])
    assert att.intervals_total == 3 and att.intervals_used == 1 and att.intervals_excluded == 2
    assert att.total_shortfall_kwh == Decimal("10.000")
    assert att.data_coverage == Decimal("0.3333")


def test_export_cap_limits_sellable_expected():
    att = attribute_losses([h(0, 120, 80, cap=100)])
    assert att.expected_kwh == Decimal("100.000")
    assert att.total_shortfall_kwh == Decimal("20.000")


def test_expected_scale_propagates_uncertainty():
    hours = [h(0, 100, 90)]
    lo, base, hi = (attribute_losses(hours, expected_scale=s).total_shortfall_kwh for s in (0.95, 1, 1.05))
    assert (lo, base, hi) == (Decimal("5.000"), Decimal("10.000"), Decimal("15.000"))


def test_merge_intervals_and_duration():
    a = datetime(2026, 1, 1, 0, tzinfo=UTC)
    iv = [(a, a + timedelta(hours=4)), (a + timedelta(hours=2), a + timedelta(hours=6)), (a + timedelta(hours=10), a + timedelta(hours=11))]
    assert len(merge_intervals(iv)) == 2
    assert total_duration_hours(iv) == Decimal("7.00")


def tariff(**kw):
    base = dict(kind="ppa", price_per_kwh=Decimal("0.10"), currency="USD", valid_from=date(2025, 1, 1))
    base.update(kw)
    return Tariff(**base)


def test_value_losses_distinguishes_production_and_revenue():
    att = attribute_losses([h(0, 1000, 0, events={"curtailment"}), h(1, 1000, 500, events={"outage"})])
    v = value_losses(att, tariff(curtailment_compensated=True), date(2025, 6, 1))
    assert v["gross_value"] == Decimal("150.00")
    assert v["lost_revenue"] == Decimal("50.00")  # curtailment compensated: production lost, revenue not
    cur = v["categories"]["curtailment"]
    assert cur["lost_production_kwh"]["value"] == 1000
    assert cur["lost_revenue"]["value"] == 0
    assert cur["gross_value"]["basis"] == Basis.MODELLED.value
    assert cur["addressable_by_maintenance"] is False

    v2 = value_losses(att, tariff(curtailment_compensated=False), date(2025, 6, 1))
    assert v2["lost_revenue"] == Decimal("150.00")


def test_tariff_escalation_and_selection():
    t1 = tariff(escalation_pct_per_year=Decimal("2"))
    assert t1.price_on(date(2027, 1, 5)) == Decimal("0.10") * Decimal("1.02") ** 2
    t2 = tariff(price_per_kwh=Decimal("0.12"), valid_from=date(2026, 1, 1))
    t1b = tariff(valid_to=date(2025, 12, 31))
    assert effective_price([t1b, t2], date(2026, 3, 1)).price_per_kwh == Decimal("0.12")
    assert effective_price([t1b, t2], date(2025, 3, 1)).price_per_kwh == Decimal("0.10")
    with pytest.raises(ValueError):
        effective_price([t2], date(2024, 1, 1))


def test_currency_conversion_requires_dated_rate():
    rates = [FxRate("USD", "KES", Decimal("129.50"), date(2026, 1, 1), "CBK"),
             FxRate("USD", "KES", Decimal("130.00"), date(2026, 2, 1), "CBK")]
    amt, r = convert(Decimal("100"), "USD", "KES", rates, date(2026, 1, 15))
    assert amt == Decimal("12950.00") and r.rate_date == date(2026, 1, 1)
    amt, _ = convert(Decimal("13000"), "KES", "USD", rates, date(2026, 3, 1))
    assert amt == Decimal("100.00")
    with pytest.raises(ValueError):
        convert(Decimal("1"), "USD", "KES", rates, date(2025, 12, 1))
    assert convert(Decimal("5"), "USD", "USD", [], date(2026, 1, 1))[0] == Decimal("5")


def test_simulate_recovery_payback_roi_npv():
    r = simulate_recovery(RecoveryInput(
        annual_addressable_loss_kwh=Decimal("100000"), recovery_rate=Decimal("0.8"), energy_price=Decimal("0.10"),
        repair_cost=Decimal("4000"), horizon_years=3, discount_rate=Decimal("0"), annual_degradation_pct=Decimal("0")))
    assert r["annual_recovered_kwh"] == Decimal("80000.000")
    assert r["annual_recovered_value"] == Decimal("8000.00")
    assert r["net_benefit"] == Decimal("20000.00")  # 3 × 8000 − 4000
    assert r["npv"] == Decimal("20000.00")
    assert r["payback_years"] == Decimal("0.50")
    assert r["roi"] == Decimal("5.0000")


def test_simulate_recovery_delay_incremental_cost_and_no_payback():
    r = simulate_recovery(RecoveryInput(
        annual_addressable_loss_kwh=Decimal("10000"), recovery_rate=Decimal("0.5"), energy_price=Decimal("0.08"),
        repair_cost=Decimal("5000"), incremental_annual_cost=Decimal("100"), days_to_effect=73, horizon_years=5,
        discount_rate=Decimal("0.08"), annual_degradation_pct=Decimal("0")))
    assert r["cash_flows"][0]["value"] == pytest.approx(320.0)  # 400 × (292/365)
    assert r["payback_years"] is None
    assert r["net_benefit"] < 0 and r["npv"] < r["net_benefit"]


def test_simulate_recovery_validates_inputs():
    with pytest.raises(ValueError):
        simulate_recovery(RecoveryInput(Decimal(1), Decimal("1.5"), Decimal(1), Decimal(1)))


def test_scenarios_ordered_and_sensitivity_sorted():
    sc = default_scenarios(5, 0.8, 10)
    assert [s.name for s in sc] == ["low", "base", "high"]
    assert sc[0].expected_scale < sc[1].expected_scale < sc[2].expected_scale
    assert sc[0].recovery_rate == Decimal("0.6") and sc[2].recovery_rate == Decimal("0.9")
    tor = sensitivity(RecoveryInput(Decimal("50000"), Decimal("0.8"), Decimal("0.1"), Decimal("2000"), days_to_effect=14))
    assert tor == sorted(tor, key=lambda r: -r["swing"])
    assert {t["driver"] for t in tor} == {"energy_price", "recovery_rate", "repair_cost", "annual_addressable_loss_kwh", "days_to_effect"}


def test_annualise():
    assert annualise(Decimal("1000"), 73) == Decimal("5000.000")
    with pytest.raises(ValueError):
        annualise(Decimal(1), 0)
