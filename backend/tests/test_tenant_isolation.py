"""Tenant separation is enforced by the API layer and, independently, by PostgreSQL row-level security."""
from sqlalchemy import func, select, text

from app.db import SessionLocal, set_tenant, system_session
from app.models import Alert, EnergyReading, Organization, Plant, User


def test_api_cannot_see_other_tenant(client, admin, other_admin, plants):
    other_plants = client.get("/api/v1/plants", headers=other_admin).json()
    assert [p["code"] for p in other_plants] == ["GH-ACC-01"]
    nai = plants["KE-NAI-01"]["id"]
    for path in (f"/api/v1/plants/{nai}", f"/api/v1/plants/{nai}/equipment", f"/api/v1/plants/{nai}/events",
                 f"/api/v1/plants/{nai}/contracts", f"/api/v1/plants/{nai}/reports/maintenance.csv"):
        assert client.get(path, headers=other_admin).status_code in (404, 402), path
    # alerts / work orders of the demo org are invisible to the other tenant
    assert client.get("/api/v1/alerts", headers=other_admin).status_code == 402  # starter plan: no anomaly module
    assert client.get("/api/v1/work-orders", headers=other_admin).json() == []
    # cannot write into another tenant's plant
    r = client.post(f"/api/v1/plants/{nai}/events", headers=other_admin,
                    json={"category": "outage", "start_ts": "2026-10-01T00:00:00Z", "end_ts": "2026-10-01T02:00:00Z"})
    assert r.status_code == 404


def _counts(org_id):
    db = SessionLocal()
    try:
        set_tenant(db, org_id)
        return (db.scalar(select(func.count(Plant.id))), db.scalar(select(func.count(EnergyReading.id))),
                db.scalar(select(func.count(Alert.id))), db.scalar(select(func.count(Organization.id))),
                set(db.scalars(select(Plant.organization_id))))
    finally:
        db.close()


def test_row_level_security_restricts_raw_queries(seeded):
    with system_session() as s:
        demo = s.scalar(select(Organization).where(Organization.slug == "demo-energy"))
        other = s.scalar(select(Organization).where(Organization.slug == "other-tenant"))
        all_orgs = list(s.scalars(select(Organization.id)))
        total_plants = s.scalar(select(func.count(Plant.id)))
    plants, readings, alerts, orgs, owners = _counts(demo.id)
    assert plants == 3 and readings > 0 and alerts > 0 and orgs == 1 and owners == {demo.id}
    plants_o, readings_o, alerts_o, orgs_o, owners_o = _counts(other.id)
    assert plants_o == 1 and alerts_o == 0 and owners_o == {other.id}
    # Every tenant (including ones created by other tests) sees exactly its own plants, and nothing else.
    seen = 0
    for org_id in all_orgs:
        n, _, _, visible_orgs, owner_ids = _counts(org_id)
        assert visible_orgs == 1 and owner_ids <= {org_id}
        seen += n
    assert seen == total_plants
    # No tenant context at all -> nothing is visible.
    assert _counts(None)[:4] == (0, 0, 0, 0)


def test_rls_blocks_cross_tenant_writes(seeded):
    with system_session() as s:
        demo = s.scalar(select(Organization).where(Organization.slug == "demo-energy"))
        other = s.scalar(select(Organization).where(Organization.slug == "other-tenant"))
    db = SessionLocal()
    try:
        set_tenant(db, other.id)
        db.add(User(organization_id=demo.id, email="intruder@example.com", full_name="x", password_hash="x", role="analyst"))
        try:
            db.flush()
            raise AssertionError("cross-tenant insert must be rejected by RLS")
        except Exception as exc:  # noqa: BLE001
            assert "row-level security" in str(exc)
        db.rollback()
        set_tenant(db, other.id)
        updated = db.execute(text("UPDATE plants SET name = 'hacked' WHERE organization_id = :o"), {"o": demo.id}).rowcount
        assert updated == 0
    finally:
        db.rollback()
        db.close()


def test_token_cannot_be_reused_across_tenants(client, admin, other_admin):
    me = client.get("/api/v1/auth/me", headers=admin).json()
    other = client.get("/api/v1/auth/me", headers=other_admin).json()
    assert me["organization"]["id"] != other["organization"]["id"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer forged.token.value"}).status_code == 401
