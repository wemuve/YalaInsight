"""End-to-end API tests against the seeded synthetic portfolio."""
import hashlib
import hmac

from app.modules.integrations.webhooks import sign

API = "/api/v1"


def test_health_and_ready(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/ready").json()
    assert r["status"] == "ready" and r["db_revision"] == "0001"


def test_login_failures_and_audit(client, admin):
    assert client.post(f"{API}/auth/login", json={"email": "admin@demo.yalainsight.local", "password": "wrong"}).status_code == 401
    assert client.get(f"{API}/plants").status_code == 401
    actions = [a["action"] for a in client.get(f"{API}/audit-logs", headers=admin).json()]
    assert "auth.login_failed" in actions and "auth.login" in actions


def test_rbac(client, login, plants):
    viewer, tech, analyst = (login(f"{u}@demo.yalainsight.local") for u in ("viewer", "tech", "analyst"))
    body = {"name": "X", "code": "X1", "dc_capacity_kwp": 100, "ac_capacity_kw": 80, "latitude": 1, "longitude": 1}
    assert client.post(f"{API}/plants", headers=viewer, json=body).status_code == 403
    assert client.get(f"{API}/plants", headers=viewer).status_code == 200
    assert client.post(f"{API}/finance/recovery-simulation", headers=tech, json={
        "annual_addressable_loss_kwh": 1, "recovery_rate": 1, "energy_price": 1, "repair_cost": 1}).status_code == 403
    assert client.get(f"{API}/users", headers=analyst).status_code == 403
    pid = plants["KE-NAI-01"]["id"]
    assert client.post(f"{API}/plants/{pid}/anomaly-detection", headers=viewer, json={}).status_code == 403


def test_module_licensing(client, other_admin):
    pid = client.get(f"{API}/plants", headers=other_admin).json()[0]["id"]
    r = client.post(f"{API}/plants/{pid}/financial-analysis", headers=other_admin, json={})
    assert r.status_code == 402 and "finance" in r.json()["detail"]
    assert client.get(f"{API}/plants/{pid}/performance", headers=other_admin).status_code == 200
    # starter plan allows 1 plant
    body = {"name": "Second", "code": "GH-2", "dc_capacity_kwp": 100, "ac_capacity_kw": 80, "latitude": 5.6, "longitude": -0.2}
    assert client.post(f"{API}/plants", headers=other_admin, json=body).status_code == 402


def test_dashboard_traceable(client, admin):
    d = client.get(f"{API}/dashboard/summary", headers=admin).json()
    p = d["portfolio"]
    assert p["plant_count"] == 3 and p["dc_capacity_kwp"] == 18000
    assert p["loss_currency"] == "USD" and "ZAR" in p["fx_rates_used"]
    assert p["actual_kwh"] > 0 and p["expected_kwh"] > p["actual_kwh_compared"]
    assert d["basis"]["carbon"].startswith("estimated")
    assert len(d["plants"]) == 3 and all(r["loss"] for r in d["plants"])


def test_performance_granularities(client, admin, plants):
    pid = plants["KE-NAI-01"]["id"]
    for g in ("hour", "day", "month", "year"):
        r = client.get(f"{API}/plants/{pid}/performance?granularity={g}", headers=admin)
        assert r.status_code == 200, r.text
        s = r.json()["summary"]
        assert 0.5 < s["performance_ratio"] < 0.9 and s["data_coverage"] > 0.95
    assert client.get(f"{API}/plants/{pid}/performance?start=2026-10-01T00:00:00", headers=admin).status_code == 422


def test_financial_analysis(client, admin, plants):
    pid = plants["KE-NAI-01"]["id"]
    r = client.post(f"{API}/plants/{pid}/financial-analysis", headers=admin, json={
        "start": "2026-09-15T00:00:00Z", "end": "2026-10-15T00:00:00Z", "report_currency": "KES",
        "recovery": {"repair_cost": 4000}})
    assert r.status_code == 200, r.text
    a = r.json()
    base, low, high = a["scenarios"]["base"], a["scenarios"]["low"], a["scenarios"]["high"]
    cats = base["losses"]
    total = sum(v["lost_production_kwh"]["value"] for v in cats.values())
    assert abs(total - base["energy"]["shortfall_kwh"]["value"]) < 0.01  # no double counting
    assert cats["inverter_failure"]["lost_production_kwh"]["value"] > 10000  # INV-03 outage attributed
    assert cats["curtailment"]["lost_production_kwh"]["value"] > 0
    assert cats["curtailment"]["lost_revenue"]["value"] == 0  # PPA compensates curtailment
    assert base["lost_revenue"]["value"] < base["gross_lost_energy_value"]["value"]
    assert low["energy"]["shortfall_kwh"]["value"] < base["energy"]["shortfall_kwh"]["value"] < high["energy"]["shortfall_kwh"]["value"]
    assert base["gross_lost_energy_value"]["converted"]["currency"] == "KES"
    # INV-03 fault was resolved before the run-rate window: not projected as recoverable
    assert base["recovery"]["addressable_loss_in_run_rate_window_kwh"] < 0.1 * base["recovery"]["addressable_loss_in_period_kwh"]
    assert base["recovery"]["basis"] == "simulated" and base["energy"]["actual_kwh"]["basis"] == "measured"
    assert a["sensitivity"] and a["assumptions"]


def test_financial_analysis_requires_contract(client, admin):
    p = client.post(f"{API}/plants", headers=admin, json={"name": "No contract", "code": "NC-1", "dc_capacity_kwp": 100,
                                                           "ac_capacity_kw": 90, "latitude": -1.0, "longitude": 36.0}).json()
    r = client.post(f"{API}/plants/{p['id']}/financial-analysis", headers=admin, json={})
    assert r.status_code == 422 and "contract" in r.json()["detail"].lower()
    assert client.delete(f"{API}/plants/{p['id']}", headers=admin).status_code == 204


def test_recovery_simulation_endpoint(client, admin):
    r = client.post(f"{API}/finance/recovery-simulation", headers=admin, json={
        "annual_addressable_loss_kwh": 100000, "recovery_rate": 0.8, "energy_price": 0.1, "repair_cost": 4000,
        "horizon_years": 3, "discount_rate": 0, "annual_degradation_pct": 0})
    res = r.json()["result"]
    assert res["net_benefit"] == 20000 and res["payback_years"] == 0.5


def test_alerts_review_and_work_order_flow(client, admin, login, plants):
    pid = plants["MA-BEN-01"]["id"]
    alerts = client.get(f"{API}/alerts?plant_id={pid}", headers=admin).json()
    rules = {a["rule_id"] for a in alerts}
    assert {"PLANT_ZERO_OUTPUT", "INVERTER_UNDERPERFORMANCE"} <= rules
    a = next(x for x in alerts if x["rule_id"] == "PLANT_ZERO_OUTPUT")
    assert a["fault_confirmed"] is False and "not a confirmed fault" in a["explanation"]
    # confirming requires inspection notes
    assert client.post(f"{API}/alerts/{a['id']}/review", headers=admin, json={"status": "confirmed"}).status_code == 422
    r = client.post(f"{API}/alerts/{a['id']}/review", headers=admin, json={"status": "confirmed", "finding_notes": "MV breaker trip found"})
    assert r.json()["fault_confirmed"] is True and r.json()["basis"] == "verified"
    stats = client.get(f"{API}/alerts/stats", headers=admin).json()["by_rule"]["PLANT_ZERO_OUTPUT"]
    assert stats["confirmed"] >= 1 and stats["precision"] == 1.0
    # detection is idempotent
    r1 = client.post(f"{API}/plants/{pid}/anomaly-detection", headers=admin, json={}).json()
    assert r1["created"] == 0
    tech = login("tech@demo.yalainsight.local")
    wo = client.post(f"{API}/work-orders", headers=admin, json={"plant_id": pid, "alert_id": a["id"], "title": "Reset breaker",
                                                                 "estimated_cost": 500, "due_date": "2026-10-20"}).json()
    assert wo["status"] == "open"
    assert client.patch(f"{API}/work-orders/{wo['id']}", headers=tech, json={"status": "completed"}).status_code == 422
    assert client.patch(f"{API}/work-orders/{wo['id']}", headers=tech, json={"status": "in_progress"}).status_code == 200
    assert client.patch(f"{API}/work-orders/{wo['id']}", headers=tech, json={"status": "completed", "actual_cost": 450}).status_code == 200
    assert client.patch(f"{API}/work-orders/{wo['id']}", headers=tech, json={"status": "verified"}).status_code == 403
    detail = client.get(f"{API}/work-orders/{wo['id']}", headers=admin).json()
    assert [e["kind"] for e in detail["events"]][:1] == ["created"] and len(detail["events"]) >= 3


def test_post_repair_evaluation(client, admin, plants):
    wos = client.get(f"{API}/work-orders?plant_id={plants['KE-NAI-01']['id']}", headers=admin).json()
    wo = next(w for w in wos if "INV-03" in w["title"])
    r = client.post(f"{API}/work-orders/{wo['id']}/evaluate-recovery", headers=admin).json()
    assert r["status"] == "evaluated" and r["basis"] == "measured"
    assert r["before"]["performance_index"] < 0.1 < 0.9 < r["after"]["performance_index"]
    assert r["realised_recovery_kwh_per_year"] > 0 and r["variance_vs_estimate_pct"] is not None


def test_csv_import_validation_and_duplicates(client, admin, plants):
    pid = plants["KE-NAI-01"]["id"]
    csv = ("timestamp,energy_kwh,equipment\n"
           "2026-10-20T08:00:00Z,100,\n2026-10-20T09:00:00Z,200,\n2026-10-20T09:00:00Z,200,\n"
           "2026-10-20T10:00:00Z,-5,\nnot-a-date,1,\n2026-10-20T11:00:00Z,99999,\n2026-10-20T12:00:00Z,50,UNKNOWN\n")
    files = {"file": ("e.csv", csv, "text/csv")}
    dry = client.post(f"{API}/plants/{pid}/imports?kind=energy&dry_run=true", headers=admin, files=files).json()
    assert dry["dry_run"] and dry["valid_rows"] == 2 and dry["duplicates_in_file"] == 1 and dry["rejected_rows"] == 5
    r = client.post(f"{API}/plants/{pid}/imports?kind=energy", headers=admin, files=files).json()
    assert r["rows_imported"] == 2
    again = client.post(f"{API}/plants/{pid}/imports?kind=energy", headers=admin, files=files).json()
    assert again["rows_imported"] == 0 and again["duplicates_existing"] == 2
    assert any("already imported" in w for w in again["warnings"])
    missing = client.post(f"{API}/plants/{pid}/imports?kind=weather", headers=admin, files={"file": ("w.csv", "ts,x\n1,2\n", "text/csv")})
    assert missing.status_code == 422 and missing.json()["detail"]["errors"][0]["message"] == "required column missing"


def test_weather_import_triggers_model_run(client, admin, plants):
    pid = plants["ZA-KTH-01"]["id"]
    csv = "timestamp,poa_wm2,ambient_temp_c\n2026-10-20T10:00:00+02:00,800,25\n2026-10-20T11:00:00,850,26\n"
    r = client.post(f"{API}/plants/{pid}/imports?kind=weather", headers=admin, files={"file": ("w.csv", csv, "text/csv")}).json()
    assert r["rows_imported"] == 2 and r["model_run"]["hours_modelled"] == 2


def test_reports_and_exports(client, admin, plants):
    pid = plants["ZA-KTH-01"]["id"]
    for kind in ("performance", "financial", "maintenance", "carbon"):
        r = client.get(f"{API}/plants/{pid}/reports/{kind}.pdf", headers=admin)
        assert r.status_code == 200 and r.content[:4] == b"%PDF", kind
        c = client.get(f"{API}/plants/{pid}/reports/{kind}.csv", headers=admin)
        assert c.status_code == 200 and kind in c.text.splitlines()[0]
    e = client.get(f"{API}/plants/{pid}/exports/expected.csv", headers=admin)
    assert "yala-poa-temperature" in e.text


def test_carbon_separates_estimated_and_verified(client, admin, plants):
    pid = plants["ZA-KTH-01"]["id"]
    c = client.get(f"{API}/plants/{pid}/carbon", headers=admin).json()
    assert c["estimate"]["verified"] is False and c["estimate"]["factor"]["is_sample"] is True
    assert c["estimate"]["warnings"]
    r = client.post(f"{API}/verified-reductions", headers=admin, json={"plant_id": pid, "period_start": "2026-01-01",
                    "period_end": "2026-06-30", "tco2e": 1234.5, "standard": "Test Standard", "verifier": "Test Verifier"})
    assert r.status_code == 201 and r.json()["basis"] == "verified"
    port = client.get(f"{API}/carbon/portfolio?start=2025-01-01T00:00:00Z&end=2026-10-15T00:00:00Z", headers=admin).json()
    assert port["verified_tco2e"] == 1234.5 and port["estimated_avoided_tco2e"] > 0


def test_gis(client, admin, plants):
    fc = client.get(f"{API}/gis/plants", headers=admin).json()
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 3
    near = client.get(f"{API}/gis/plants/near?lat=-0.7&lon=36.4&radius_km=50", headers=admin).json()
    assert [n["code"] for n in near] == ["KE-NAI-01"]
    within = client.get(f"{API}/gis/plants/within?bbox=-20,20,0,40", headers=admin).json()
    assert [w["code"] for w in within] == ["MA-BEN-01"]
    assets = client.get(f"{API}/gis/plants/{plants['KE-NAI-01']['id']}/assets", headers=admin).json()
    kinds = {f["properties"]["kind"] for f in assets["features"]}
    assert {"plant", "boundary", "inverter"} <= kinds
    assert len(client.get(f"{API}/gis/regions", headers=admin).json()) == 3


def test_forecast(client, admin, plants):
    pid = plants["KE-NAI-01"]["id"]
    f = client.get(f"{API}/plants/{pid}/forecast?hours=24", headers=admin).json()
    assert len(f["points"]) == 24 and f["total_p50_kwh"] > 0
    b = client.get(f"{API}/plants/{pid}/forecast/backtest?days=7", headers=admin).json()
    assert b["hours_scored"] > 0 and b["nrmse"] is not None


def test_api_key_access_and_revocation(client, admin):
    k = client.post(f"{API}/api-keys", headers=admin, json={"name": "SCADA bridge", "role": "read_only"}).json()
    h = {"X-API-Key": k["key"]}
    assert client.get(f"{API}/plants", headers=h).status_code == 200
    assert client.post(f"{API}/plants", headers=h, json={}).status_code in (403, 422)
    assert client.get(f"{API}/plants", headers={"X-API-Key": k["key"] + "x"}).status_code == 401
    client.delete(f"{API}/api-keys/{k['id']}", headers=admin)
    assert client.get(f"{API}/plants", headers=h).status_code == 401


def test_webhook_signature():
    body = b'{"event":"x"}'
    assert sign("s3cret", body) == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()


def test_webhook_subscription_validation(client, admin):
    assert client.post(f"{API}/webhooks", headers=admin, json={"url": "http://insecure.example", "events": ["alert.created"]}).status_code == 422
    r = client.post(f"{API}/webhooks", headers=admin, json={"url": "https://example.invalid/hook", "events": ["alert.created"]})
    assert r.status_code == 201 and len(r.json()["secret"]) == 64
    assert "secret" not in client.get(f"{API}/webhooks", headers=admin).json()[0]
    assert client.delete(f"{API}/webhooks/{r.json()['id']}", headers=admin).status_code == 204


def test_platform_admin_licensing(client, login):
    pa = login("platform@yalainsight.local", "Platform-Admin-2026")
    orgs = {o["slug"]: o for o in client.get(f"{API}/platform/organizations", headers=pa).json()}
    other = orgs["other-tenant"]
    r = client.patch(f"{API}/platform/organizations/{other['id']}/license", headers=pa, json={"module_overrides": {"finance": True}})
    assert "finance" in r.json()["modules"]
    client.patch(f"{API}/platform/organizations/{other['id']}/license", headers=pa, json={"module_overrides": {"finance": None}})
    admin = login("admin@demo.yalainsight.local")
    assert client.get(f"{API}/platform/organizations", headers=admin).status_code == 403


def test_onboarding_flow_with_sample_files(client, admin):
    """Register plant -> contract -> weather -> energy -> events -> detection -> financial analysis."""
    import pathlib
    sd = pathlib.Path(__file__).resolve().parents[2] / "sample_data"
    p = client.post(f"{API}/plants", headers=admin, json={"name": "Nairobi Sample", "code": "KE-NBO-S", "dc_capacity_kwp": 1000,
                    "ac_capacity_kw": 850, "latitude": -1.29, "longitude": 36.82, "timezone": "Africa/Nairobi", "country_code": "KE"}).json()
    pid = p["id"]
    assert client.post(f"{API}/plants/{pid}/contracts", headers=admin, json={"kind": "ppa", "price_per_kwh": 0.09, "currency": "USD",
                       "valid_from": "2026-01-01"}).status_code == 201
    for kind, name in (("weather", "weather_hourly_7d.csv"), ("energy", "energy_meter_hourly_7d.csv"), ("events", "events_sample.csv")):
        r = client.post(f"{API}/plants/{pid}/imports?kind={kind}", headers=admin, files={"file": (name, (sd / name).read_bytes(), "text/csv")})
        assert r.status_code == 200 and r.json()["rows_rejected"] == 0, r.text
    bad = client.post(f"{API}/plants/{pid}/imports?kind=energy&dry_run=true", headers=admin,
                      files={"file": ("x.csv", (sd / "energy_with_errors.csv").read_bytes(), "text/csv")}).json()
    assert bad["valid_rows"] == 2 and bad["rejected_rows"] == 5
    period = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-08T00:00:00Z"}
    det = client.post(f"{API}/plants/{pid}/anomaly-detection", headers=admin, json=period).json()
    assert det["created"] >= 1
    rules = {a["rule_id"] for a in client.get(f"{API}/alerts?plant_id={pid}", headers=admin).json()}
    assert "PLANT_ZERO_OUTPUT" in rules
    a = client.post(f"{API}/plants/{pid}/financial-analysis", headers=admin, json=period).json()
    base = a["scenarios"]["base"]
    assert base["losses"]["plant_downtime"]["lost_production_kwh"]["value"] > 1000
    assert base["losses"]["curtailment"]["lost_production_kwh"]["value"] > 0
    assert a["data_quality"]["coverage"] == 1.0
    client.delete(f"{API}/plants/{pid}", headers=admin)
