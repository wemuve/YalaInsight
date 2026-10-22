"""Unit tests for the performance, anomaly, carbon, forecasting, validation and connector engines (no database)."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.ratelimit import RateLimiter
from app.core.rbac import has_permission
from app.modules.anomaly import rules
from app.modules.carbon.engine import FactorRef, avoided_emissions
from app.modules.forecasting.engine import Obs, backtest, climatology_forecast
from app.modules.ingestion.validation import Context, validate_csv
from app.modules.integrations.connectors.base import ConnectorError
from app.modules.integrations.connectors.pvgis import PVGISConnector
from app.modules.performance import engine as pe

T0 = datetime(2026, 6, 1, tzinfo=UTC)


# ---------------------------------------------------------------- performance model
def test_expected_energy_stc_and_temperature():
    p = pe.ModelParams(dc_kwp=1000, ac_kw=1000, temp_coeff_pct_per_c=-0.4, system_loss_pct=10, annual_degradation_pct=0)
    pt = pe.expected_energy(p, 1000, None, 22.0, T0)  # module 22 + 3 = 25 degC cell
    assert pt.reference_yield_kwh == 1000 and pt.cell_temp_c == pytest.approx(25)
    assert pt.expected_kwh == pytest.approx(900)
    hot = pe.expected_energy(p, 1000, None, 47.0, T0)  # cell 50 degC -> -10 %
    assert hot.expected_kwh == pytest.approx(810)
    assert pe.expected_energy(p, None, 20, None, T0) is None


def test_clipping_and_degradation():
    p = pe.ModelParams(dc_kwp=1200, ac_kw=1000, system_loss_pct=0, temp_coeff_pct_per_c=0, commissioning_date=date(2016, 6, 1),
                       annual_degradation_pct=1.0)
    assert pe.expected_energy(pe.ModelParams(dc_kwp=1200, ac_kw=1000, system_loss_pct=0, temp_coeff_pct_per_c=0), 1000, 25, None, T0).expected_kwh == 1000
    assert pe.degradation_factor(date(2016, 6, 1), date(2026, 6, 1), 1.0) == pytest.approx(0.99 ** 10, rel=1e-3)
    assert pe.expected_energy(p, 500, 25, None, T0).expected_kwh == pytest.approx(600 * 0.99 ** 10, rel=1e-3)


def test_summarize_kpis_and_gaps():
    ivs = [pe.Interval(T0 + timedelta(hours=i), a, e, r, poa) for i, (a, e, r, poa) in enumerate([
        (80, 100, 125, 600), (0, 100, 125, 600), (None, 100, 125, 600), (90, None, None, None), (0, 0, 0, 0)])]
    s = pe.summarize(ivs, dc_kwp=125)
    assert s["intervals"] == 5 and s["intervals_compared"] == 3
    assert s["actual_kwh"] == 170 and s["actual_kwh_compared"] == 80 and s["expected_kwh"] == 200
    assert s["performance_ratio"] == pytest.approx(80 / 250) and s["availability"] == 0.5
    assert s["missing_actual_intervals"] == 1 and s["missing_expected_intervals"] == 1
    assert len(pe.aggregate(ivs, "hour", 125)) == 5 and len(pe.aggregate(ivs, "day", 125)) == 1


# ---------------------------------------------------------------- anomaly rules
def _day(start, profile, inv=None, poa=None, excluded=False):
    out = []
    for h in range(24):
        e = profile(h)
        p = poa(h) if poa else (800 if e > 0 else 0)
        out.append(rules.HourPoint(start + timedelta(hours=h), e * 0.98, e, p, excluded, inv(h, e) if inv else {}))
    return out


def sun(h):
    return 100.0 if 7 <= h <= 17 else 0.0


def test_zero_output_detected_and_excluded_when_curtailed():
    hours = _day(T0, sun)
    for p in hours[9:13]:
        p.actual_kwh = 0
    f = rules.rule_plant_zero_output(hours, rules.RuleConfig())
    assert len(f) == 1 and f[0].evidence["hours"] == 4 and f[0].energy_loss_kwh == pytest.approx(400)
    assert "not a confirmed fault" in f[0].explanation
    for p in hours[9:13]:
        p.excluded = True
    assert rules.rule_plant_zero_output(hours, rules.RuleConfig()) == []


def test_inverter_offline_spans_nights():
    invs = [rules.InverterInfo(f"i{k}", f"INV-{k}", 100) for k in range(3)]
    hours = []
    for d in range(2):
        hours += _day(T0 + timedelta(days=d), sun, inv=lambda h, e: {"i0": 0.0 if e else 0.0, "i1": e / 3, "i2": e / 3})
    f = rules.rule_inverter_offline(hours, invs, rules.RuleConfig())
    assert len(f) == 1 and f[0].equipment_id == "i0" and f[0].evidence["daylight_hours"] == 22


def test_inverter_underperformance_merges_weeks():
    invs = [rules.InverterInfo(f"i{k}", f"INV-{k}", 100) for k in range(3)]
    hours = []
    for d in range(14):
        hours += _day(T0 + timedelta(days=d), sun, inv=lambda h, e: {"i0": e / 3 * 0.8, "i1": e / 3, "i2": e / 3})
    f = rules.rule_inverter_underperformance(hours, invs, rules.RuleConfig())
    assert len(f) == 1 and f[0].evidence["weeks"] >= 2 and f[0].evidence["mean_deficit_pct"] == pytest.approx(20, abs=0.5)


def test_persistent_low_pi_and_trend():
    hours = []
    for d in range(28):
        day = _day(T0 + timedelta(days=d), sun)
        for p in day:
            p.actual_kwh = p.expected_kwh * (0.99 - 0.006 * d)
        hours += day
    cfg = rules.RuleConfig()
    assert rules.rule_persistent_low_pi(hours, cfg)
    trend = rules.rule_declining_trend(hours, cfg)
    assert trend and trend[0].confidence == "low" and trend[0].evidence["slope_per_day"] == pytest.approx(-0.006, abs=1e-4)


def test_sensor_flatline_and_data_gap():
    hours = _day(T0, sun)
    for p in hours[8:14]:
        p.poa_wm2 = 612.0
    for p in hours[14:18]:
        p.actual_kwh = None
    found = {f.rule_id for f in rules.run_all(hours, [])}
    assert {"IRRADIANCE_SENSOR_FLATLINE", "DATA_GAP"} <= found


def test_priority_score_ordering():
    u1, s1 = rules.priority_score("critical", 10000, T0, T0)
    u2, s2 = rules.priority_score("low", 10, T0 - timedelta(days=20), T0)
    assert u1 == 1.0 and u2 < 0.5 and s1 > s2


# ---------------------------------------------------------------- carbon / forecast
def test_avoided_emissions():
    r = avoided_emissions(1_000_000, FactorRef("grid", Decimal("0.5"), "grid average", "src", 2024, True), date(2026, 1, 1), date(2026, 1, 31))
    assert r["estimated_avoided_tco2e"] == Decimal("500.000") and r["verified"] is False and r["warnings"]


def test_climatology_forecast_and_backtest():
    hist = [Obs(T0 + timedelta(hours=i), sun((T0 + timedelta(hours=i)).hour)) for i in range(24 * 40)]
    f = climatology_forecast(hist, T0 + timedelta(days=40), 24, 30)
    assert f["total_p50_kwh"] == pytest.approx(1100) and not f["warnings"]
    sparse = climatology_forecast(hist[::3], T0 + timedelta(days=40), 24, 30)
    assert sparse["warnings"]
    b = backtest(hist, days=5)
    assert b["mae_kwh"] == 0 and b["bias_pct"] == 0


# ---------------------------------------------------------------- validation
def test_csv_units_timezone_and_errors():
    ctx = Context(timezone="Africa/Nairobi", max_kw=1000, equipment={"inv-1": ("eq1", 250.0)})
    r = validate_csv("timestamp,energy_mwh,equipment\n2026-01-01T12:00:00,0.2,INV-1\n2026-01-01T13:00:00,0.9,inv-1\n", "energy", ctx)
    assert len(r.rows) == 1 and r.rows[0]["energy_kwh"] == pytest.approx(200)
    assert r.rows[0]["ts"] == datetime(2026, 1, 1, 9, tzinfo=UTC)  # EAT = UTC+3
    assert r.errors[0].field == "energy_mwh" and "exceeds" in r.errors[0].message
    w = validate_csv("timestamp,poa_wm2,foo\n2026-01-01T12:00:00Z,2000,1\n", "weather", Context())
    assert w.rejected == 1 and w.warnings
    ev = validate_csv("start,end,category\n2026-01-02T00:00Z,2026-01-01T00:00Z,outage\n2026-01-01T00:00Z,2026-01-01T03:00Z,bogus\n", "events", Context())
    assert ev.rejected == 2
    missing = validate_csv("time,value\n1,2\n", "energy", Context())
    assert missing.errors and missing.errors[0].row == 0


# ---------------------------------------------------------------- connectors / security
def test_pvgis_parse():
    data = {"inputs": {"meteo_data": {"radiation_db": "PVGIS-SARAH3"}},
            "outputs": {"monthly": {"fixed": [{"month": m, "E_m": 100.0 * m} for m in range(1, 13)]}, "totals": {"fixed": {"E_y": 7800.0}}}}
    est = PVGISConnector(client=object()).parse(data, {"lat": 0})
    assert est.annual_kwh == 7800 and est.monthly_kwh[12] == 1200 and "SARAH3" in est.metadata.model_version
    with pytest.raises(ConnectorError):
        PVGISConnector(client=object()).parse({"outputs": {}}, {})


def test_rbac_matrix_and_rate_limiter():
    assert has_permission("platform_admin", "anything")
    assert has_permission("technician", "workorders:execute") and not has_permission("technician", "finance:simulate")
    assert not has_permission("read_only", "plants:write") and not has_permission("unknown", "plants:read")
    rl = RateLimiter()
    assert all(rl.allow("k", 3)[0] for _ in range(3))
    ok, retry = rl.allow("k", 3)
    assert not ok and retry >= 1
