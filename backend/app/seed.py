"""Generate deterministic SYNTHETIC sample data for demos and tests.

    python -m app.seed --reset            # wipe tenant data and load the demo portfolio
    python -m app.seed --days 60          # shorter history

All generated data is synthetic and labelled as such (data source kind 'synthetic'). Injected
scenarios let every analytics module be exercised:

* Naivasha (KE): inverter INV-03 offline 5 days (unrecorded) + recorded, compensated curtailment.
* Kathu (ZA, ZAR tariff): progressive soiling followed by a cleaning; irradiance sensor flatline; planned maintenance.
* Benguerir (MA): unrecorded full-plant outage; INV-B2 persistently ~15 % low (string failures); data gap.
"""
from __future__ import annotations

import argparse
import math
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import insert, select, text

from .core.security import hash_password
from .db import system_session
from .models import (
    TENANT_TABLES,
    Alert,
    AssetRecord,
    DataSource,
    EmissionFactor,
    EnergyContract,
    EnergyReading,
    Equipment,
    ExchangeRate,
    OperationalEvent,
    Organization,
    Plant,
    User,
    WeatherReading,
    WorkOrder,
    WorkOrderEvent,
)
from .modules.anomaly.service import detect
from .modules.performance import engine as pe
from .modules.performance.service import model_params, recompute_expected

DEMO_PASSWORD = "Demo-Password-2026"
PLATFORM_PASSWORD = "Platform-Admin-2026"
DATA_END = datetime(2026, 10, 15, tzinfo=UTC)

SAMPLE_FACTORS = [  # Illustrative values only — replace with authoritative, dated factors before reporting.
    ("KE", "Kenya national grid (illustrative)", "0.200"),
    ("ZA", "South Africa Eskom grid (illustrative)", "0.950"),
    ("MA", "Morocco national grid (illustrative)", "0.700"),
    ("NG", "Nigeria national grid (illustrative)", "0.430"),
    ("GH", "Ghana national grid (illustrative)", "0.450"),
]


def solar_elevation_sin(lat: float, lon: float, ts: datetime) -> float:
    doy = ts.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(2 * math.pi * (284 + doy) / 365))
    solar_time = ts.hour + 0.5 + lon / 15.0  # interval midpoint
    ha = math.radians(15 * (solar_time - 12))
    la = math.radians(lat)
    return math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)


def weather_series(rng: random.Random, lat: float, lon: float, start: datetime, hours: int, base_temp: float) -> list[dict]:
    out, cloud = [], 0.85
    for i in range(hours):
        ts = start + timedelta(hours=i)
        if ts.hour == 0:
            cloud = min(1.0, max(0.25, 0.6 * cloud + 0.4 * rng.uniform(0.4, 1.05)))
        s = solar_elevation_sin(lat, lon, ts)
        ghi = 0.0 if s <= 0 else 1050 * s ** 1.15 * min(1.0, cloud * rng.uniform(0.92, 1.05))
        poa = ghi * 1.08 if ghi > 0 else 0.0
        local_h = (ts.hour + lon / 15) % 24
        amb = base_temp + 7 * math.sin((local_h - 9) / 24 * 2 * math.pi) + rng.uniform(-0.8, 0.8)
        out.append({"ts": ts, "ghi_wm2": round(ghi, 1), "poa_wm2": round(poa, 1), "ambient_temp_c": round(amb, 1),
                    "module_temp_c": round(amb + poa * 0.028, 1), "wind_ms": round(rng.uniform(0.5, 6), 1)})
    return out


def reset(db) -> None:  # noqa: ANN001
    db.execute(text("TRUNCATE " + ", ".join(TENANT_TABLES + ["emission_factors", "organizations"]) + " CASCADE"))
    db.commit()


def _org(db, name: str, slug: str, plan: str, currency: str, country: str | None) -> Organization:  # noqa: ANN001
    o = Organization(name=name, slug=slug, plan_code=plan, default_currency=currency, country_code=country)
    db.add(o)
    db.flush()
    return o


def _user(db, org: Organization, email: str, name: str, role: str, pw: str = DEMO_PASSWORD) -> User:  # noqa: ANN001
    u = User(organization_id=org.id, email=email, full_name=name, role=role, password_hash=hash_password(pw))
    db.add(u)
    db.flush()
    return u


def _boundary(lat: float, lon: float, d: float) -> str:
    pts = [(lon - d, lat - d), (lon + d, lat - d), (lon + d, lat + d), (lon - d, lat + d), (lon - d, lat - d)]
    return "SRID=4326;POLYGON((" + ", ".join(f"{x} {y}" for x, y in pts) + "))"


def build_plant(db, org: Organization, spec: dict, start: datetime, hours: int, rng: random.Random, src: DataSource) -> dict:  # noqa: ANN001
    lat, lon = spec["lat"], spec["lon"]
    plant = Plant(organization_id=org.id, name=spec["name"], code=spec["code"], technology=spec.get("technology", "mono-Si"),
                  dc_capacity_kwp=spec["kwp"], ac_capacity_kw=spec["kw"], commissioning_date=spec["commissioned"],
                  country_code=spec["country"], region=spec["region"], timezone=spec["tz"],
                  location=f"SRID=4326;POINT({lon} {lat})", boundary=_boundary(lat, lon, spec.get("size_deg", 0.004)),
                  tilt_deg=spec.get("tilt", 15), azimuth_deg=0, currency=spec["currency"],
                  meta={"synthetic": True})
    db.add(plant)
    db.flush()
    n = spec["inverters"]
    invs = []
    for k in range(n):
        e = Equipment(organization_id=org.id, plant_id=plant.id, kind="inverter", name=f"{spec['inv_prefix']}{k + 1}",
                      manufacturer=spec.get("inv_make", "SunGrid"), model=spec.get("inv_model", "SG-1250HV"),
                      serial_number=f"{spec['code']}-INV-{k + 1:02d}-{rng.randint(10000, 99999)}",
                      rated_power_kw=spec["kw"] / n, dc_capacity_kwp=spec["kwp"] / n, commissioning_date=spec["commissioned"],
                      warranty_end=spec["commissioned"].replace(year=spec["commissioned"].year + 5),
                      warranty_terms="5-year product warranty; extended to 10 years on service contract",
                      location=f"SRID=4326;POINT({lon - 0.002 + 0.004 * (k + 0.5) / n} {lat + 0.0005})")
        db.add(e)
        invs.append(e)
    meter = Equipment(organization_id=org.id, plant_id=plant.id, kind="meter", name="Revenue meter",
                      manufacturer="MeterCo", model="RM-4", serial_number=f"{spec['code']}-MTR-01",
                      location=f"SRID=4326;POINT({lon} {lat - 0.003})")
    ws = Equipment(organization_id=org.id, plant_id=plant.id, kind="weather_station", name="Weather station (POA pyranometer)",
                   manufacturer="Kipp & Zonen", model="SMP10", serial_number=f"{spec['code']}-WS-01",
                   location=f"SRID=4326;POINT({lon + 0.001} {lat})")
    db.add_all([meter, ws])
    db.flush()

    wx = weather_series(rng, lat, lon, start, hours, spec["temp"])
    params = model_params(plant)
    energy_rows, weather_rows = [], []
    faults = spec.get("faults", {})
    for i, w in enumerate(wx):
        ts = w["ts"]
        day = (ts - start).days
        poa_reported = w["poa_wm2"]
        if "flatline" in faults and faults["flatline"][0] <= i < faults["flatline"][0] + faults["flatline"][1] and poa_reported > 0:
            poa_reported = wx[faults["flatline"][0]]["poa_wm2"] or 612.0
        weather_rows.append({**w, "poa_wm2": poa_reported, "organization_id": org.id, "plant_id": plant.id, "source_id": src.id})
        pt = pe.expected_energy(params, w["poa_wm2"], w["ambient_temp_c"], w["module_temp_c"], ts)
        true_exp = pt.expected_kwh if pt else 0.0
        plant_factor = spec.get("true_pi", 0.985)
        if "soiling" in faults:
            s0, s1, rate = faults["soiling"]
            if s0 <= day < s1:
                plant_factor *= 1 - rate * (day - s0)
        if "outage" in faults and faults["outage"][0] <= i < faults["outage"][0] + faults["outage"][1]:
            plant_factor = 0.0
        if "planned" in faults and faults["planned"][0] <= i < faults["planned"][0] + faults["planned"][1]:
            plant_factor = 0.0
        if "gap" in faults and faults["gap"][0] <= i < faults["gap"][0] + faults["gap"][1]:
            continue
        cap = None
        for c0, clen in faults.get("curtailment", []):
            if c0 <= i < c0 + clen:
                cap = 0.55 * spec["kw"]
        total = 0.0
        for k, inv in enumerate(invs):
            f = plant_factor * rng.uniform(0.985, 1.015)
            if "inv_offline" in faults and k == faults["inv_offline"][0] and faults["inv_offline"][1] <= i < faults["inv_offline"][1] + faults["inv_offline"][2]:
                f = 0.0
            if "inv_low" in faults and k == faults["inv_low"][0] and day >= faults["inv_low"][1]:
                f *= 1 - faults["inv_low"][2]
            v = true_exp / n * f
            total += v
            energy_rows.append({"organization_id": org.id, "plant_id": plant.id, "equipment_id": inv.id, "ts": ts,
                                "energy_kwh": round(v, 3), "power_kw": round(v, 3), "source_id": src.id})
        if cap is not None and total > cap:
            scale = cap / total
            for r in energy_rows[-n:]:
                r["energy_kwh"] = round(r["energy_kwh"] * scale, 3)
                r["power_kw"] = r["energy_kwh"]
            total = cap
        energy_rows.append({"organization_id": org.id, "plant_id": plant.id, "equipment_id": None, "ts": ts,
                            "energy_kwh": round(total * 0.995, 3), "power_kw": round(total * 0.995, 3), "source_id": src.id})
    for chunk in range(0, len(energy_rows), 5000):
        db.execute(insert(EnergyReading), energy_rows[chunk:chunk + 5000])
    for chunk in range(0, len(weather_rows), 5000):
        db.execute(insert(WeatherReading), weather_rows[chunk:chunk + 5000])
    for c0, clen in faults.get("curtailment", []):
        db.add(OperationalEvent(organization_id=org.id, plant_id=plant.id, category="curtailment", confirmed=True,
                                start_ts=start + timedelta(hours=c0), end_ts=start + timedelta(hours=c0 + clen),
                                description="Grid operator dispatch instruction (export limited to 55 %)", source="import"))
    if "planned" in faults:
        p0, plen = faults["planned"]
        db.add(OperationalEvent(organization_id=org.id, plant_id=plant.id, category="planned_maintenance", confirmed=True,
                                start_ts=start + timedelta(hours=p0), end_ts=start + timedelta(hours=p0 + plen),
                                description="Planned MV switchgear maintenance"))
    c = spec["contract"]
    db.add(EnergyContract(organization_id=org.id, plant_id=plant.id, valid_from=spec["commissioned"], **c))
    db.flush()
    return {"plant": plant, "inverters": invs}


def run(days: int = 120, do_reset: bool = False) -> None:
    rng = random.Random(42)
    start = DATA_END - timedelta(days=days)
    hours = days * 24
    with system_session() as db:
        if do_reset:
            reset(db)
        elif db.scalar(select(Organization).where(Organization.slug == "demo-energy")):
            print("Demo data already present (use --reset to reload).")
            return
        for cc, name, v in SAMPLE_FACTORS:
            db.add(EmissionFactor(organization_id=None, name=name, country_code=cc, kg_co2e_per_kwh=Decimal(v),
                                  method="grid average (illustrative)", is_sample=True, year=2024,
                                  source="ILLUSTRATIVE SAMPLE VALUE — replace with an authoritative dated source, e.g. the IFI "
                                         "Harmonised Grid Emission Factor dataset or the national grid operator / registry."))
        platform = _org(db, "YalaInsight Platform", "platform", "enterprise", "USD", None)
        _user(db, platform, "platform@yalainsight.local", "Platform Administrator", "platform_admin", PLATFORM_PASSWORD)

        org = _org(db, "Sahel & Savanna Energy (demo)", "demo-energy", "enterprise", "USD", "KE")
        admin = _user(db, org, "admin@demo.yalainsight.local", "Amina Odhiambo", "client_admin")
        _user(db, org, "owner@demo.yalainsight.local", "Kwame Mensah", "plant_owner")
        analyst = _user(db, org, "analyst@demo.yalainsight.local", "Lerato Dlamini", "analyst")
        tech = _user(db, org, "tech@demo.yalainsight.local", "Youssef Benali", "technician")
        _user(db, org, "viewer@demo.yalainsight.local", "Investor Viewer", "read_only")
        src = DataSource(organization_id=org.id, name="Synthetic demo telemetry", kind="synthetic", provider="YalaInsight seed",
                         license_notes="Synthetic data — not real plant measurements.", coverage_notes="Demo plants only",
                         model_version="seed-1", retrieved_at=datetime.now(UTC))
        db.add(src)
        db.flush()
        h = 24
        specs = [
            {"name": "Naivasha Solar Park", "code": "KE-NAI-01", "lat": -0.7167, "lon": 36.4333, "kwp": 5000, "kw": 4200,
             "commissioned": date(2022, 3, 1), "country": "KE", "region": "Nakuru County", "tz": "Africa/Nairobi",
             "temp": 21, "inverters": 4, "inv_prefix": "INV-0", "currency": "USD",
             "contract": {"kind": "ppa", "counterparty": "Kenya Power (sample)", "price_per_kwh": Decimal("0.0850"), "currency": "USD",
                          "curtailment_compensated": True, "escalation_pct_per_year": Decimal("1.5")},
             "faults": {"inv_offline": (2, (days - 12) * h, 5 * h), "curtailment": [((days - 40) * h + 11, 3), ((days - 25) * h + 12, 3),
                                                                                    ((days - 9) * h + 10, 4)]}},
            {"name": "Kathu Solar Farm", "code": "ZA-KTH-01", "lat": -27.6950, "lon": 23.0500, "kwp": 10000, "kw": 8500,
             "commissioned": date(2020, 9, 15), "country": "ZA", "region": "Northern Cape", "tz": "Africa/Johannesburg",
             "temp": 22, "inverters": 4, "inv_prefix": "INV-K", "currency": "ZAR", "size_deg": 0.006,
             "contract": {"kind": "feed_in_tariff", "counterparty": "Eskom (sample REIPPPP-style)", "price_per_kwh": Decimal("1.4500"),
                          "currency": "ZAR", "curtailment_compensated": False, "escalation_pct_per_year": Decimal("4.0")},
             "faults": {"soiling": (days - 50, days - 15, 0.004), "flatline": ((days - 60) * h + 8, 6),
                        "planned": ((days - 30) * h + 7, 5)}},
            {"name": "Benguerir Solar Plant", "code": "MA-BEN-01", "lat": 32.2360, "lon": -7.9530, "kwp": 3000, "kw": 2500,
             "commissioned": date(2021, 5, 20), "country": "MA", "region": "Rehamna", "tz": "Africa/Casablanca",
             "temp": 24, "inverters": 3, "inv_prefix": "INV-B", "currency": "USD", "true_pi": 0.975,
             "contract": {"kind": "self_consumption", "counterparty": "On-site phosphate processing load (sample)",
                          "price_per_kwh": Decimal("0.1100"), "currency": "USD", "curtailment_compensated": False},
             "faults": {"outage": ((days - 20) * h + 8, 7), "inv_low": (1, days - 45, 0.15), "gap": ((days - 33) * h + 9, 8)}},
        ]
        built = [build_plant(db, org, s, start, hours, rng, src) for s in specs]
        db.add_all([
            ExchangeRate(organization_id=org.id, base="USD", quote="ZAR", rate=Decimal("17.80"), rate_date=date(2026, 9, 1),
                         source="Illustrative sample rate — replace with central bank reference rate"),
            ExchangeRate(organization_id=org.id, base="USD", quote="ZAR", rate=Decimal("17.65"), rate_date=date(2026, 10, 1),
                         source="Illustrative sample rate — replace with central bank reference rate"),
            ExchangeRate(organization_id=org.id, base="USD", quote="KES", rate=Decimal("129.20"), rate_date=date(2026, 10, 1),
                         source="Illustrative sample rate — replace with central bank reference rate"),
        ])
        db.flush()
        for b in built:
            recompute_expected(db, b["plant"])
        db.flush()
        for b in built:
            p = b["plant"]
            db.add(AssetRecord(organization_id=org.id, plant_id=p.id, kind="commissioning", title="Plant commissioning & EPC handover",
                               contractor="Sample EPC Ltd", performed_at=p.commissioning_date,
                               notes="As-built documentation, IV-curve tests and PR test accepted.", data={"pr_test": 0.81}))
            db.add(AssetRecord(organization_id=org.id, plant_id=p.id, kind="inspection", title="Annual thermographic inspection",
                               contractor="Thermal Survey Co (sample)", performed_at=date(2026, 6, 10),
                               notes="No critical hotspots; 3 modules with minor bypass-diode heating."))
            detect(db, p, start, DATA_END)
        db.flush()
        nai = built[0]
        inv3 = nai["inverters"][2]
        fault_start = start + timedelta(hours=(days - 12) * h)
        inv3_alert = db.scalar(select(Alert).where(Alert.equipment_id == inv3.id, Alert.rule_id == "INVERTER_OFFLINE"))
        if inv3_alert:
            inv3_alert.status, inv3_alert.finding_notes = "resolved", "IGBT failure confirmed on site; module replaced."
        wo = WorkOrder(organization_id=org.id, plant_id=nai["plant"].id, equipment_id=inv3.id,
                       alert_id=inv3_alert.id if inv3_alert else None,
                       title="INV-03 offline — replace failed IGBT module", kind="corrective", priority="high",
                       status="completed", assignee_id=tech.id, contractor="SunGrid Service Africa (sample)",
                       created_by=analyst.id, due_date=(fault_start + timedelta(days=4)).date(),
                       started_at=fault_start + timedelta(days=3), completed_at=fault_start + timedelta(days=5, hours=2),
                       currency="USD", estimated_cost=Decimal("3800"), actual_cost=Decimal("3450"),
                       est_recovery_kwh_per_year=1_800_000 / 4 * 0.9, est_recovery_value_per_year=Decimal("34425"))
        db.add(wo)
        db.flush()
        for kind, msg in [("created", wo.title), ("diagnosis", "IGBT failure confirmed from fault code F-214"),
                          ("repair", "Power module replaced under warranty; labour charged"), ("status_change", "Completed")]:
            db.add(WorkOrderEvent(organization_id=org.id, work_order_id=wo.id, kind=kind, message=msg, user_id=tech.id))
        db.add(AssetRecord(organization_id=org.id, plant_id=nai["plant"].id, equipment_id=inv3.id, work_order_id=wo.id,
                           kind="corrective_action", title="IGBT power module replacement", contractor="SunGrid Service Africa (sample)",
                           performed_at=(fault_start + timedelta(days=5)).date(), notes="Warranty claim WC-2026-117 (sample)."))
        ben = built[2]
        db.add(WorkOrder(organization_id=org.id, plant_id=ben["plant"].id, equipment_id=ben["inverters"][1].id,
                         title="INV-B2 underperformance — string-level IV-curve inspection", kind="inspection", priority="medium",
                         status="assigned", assignee_id=tech.id, created_by=admin.id, due_date=date(2026, 10, 10), currency="USD",
                         estimated_cost=Decimal("1200"), est_recovery_kwh_per_year=0.15 * 1_700_000 / 3, est_recovery_value_per_year=Decimal("9350")))
        kth = built[1]
        db.add(WorkOrder(organization_id=org.id, plant_id=kth["plant"].id, title="Quarterly module cleaning", kind="preventive",
                         priority="medium", status="open", created_by=admin.id, due_date=date(2026, 11, 5), currency="ZAR",
                         estimated_cost=Decimal("85000")))

        other = _org(db, "Other Tenant Ltd (isolation demo)", "other-tenant", "starter", "USD", "GH")
        _user(db, other, "admin@other.yalainsight.local", "Other Admin", "client_admin")
        src2 = DataSource(organization_id=other.id, name="Synthetic demo telemetry", kind="synthetic", provider="YalaInsight seed")
        db.add(src2)
        db.flush()
        o = build_plant(db, other, {"name": "Accra Rooftop Portfolio", "code": "GH-ACC-01", "lat": 5.6037, "lon": -0.1870,
                                    "kwp": 800, "kw": 700, "commissioned": date(2023, 2, 1), "country": "GH", "region": "Greater Accra",
                                    "tz": "Africa/Accra", "temp": 27, "inverters": 2, "inv_prefix": "INV-A", "currency": "USD",
                                    "size_deg": 0.001,
                                    "contract": {"kind": "self_consumption", "price_per_kwh": Decimal("0.16"), "currency": "USD"}},
                        DATA_END - timedelta(days=14), 14 * 24, rng, src2)
        recompute_expected(db, o["plant"])
        db.commit()
    print(f"Loaded synthetic demo data: {days} days ending {DATA_END.date()}.")
    print(f"  Demo login: admin@demo.yalainsight.local / {DEMO_PASSWORD}  (also owner@, analyst@, tech@, viewer@)")
    print(f"  Platform admin: platform@yalainsight.local / {PLATFORM_PASSWORD}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    run(a.days, a.reset)
