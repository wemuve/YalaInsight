"""Production-hardening features: lockout, 2FA, session revocation, forced password change, plant scoping,
webhook retries, background jobs, security headers and first-run bootstrap."""
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import _db_url
from app.core import totp
from app.db import system_session
from app.models import User, WebhookDelivery, WebhookSubscription

API = "/api/v1"
PW = "Initial-Passw0rd"


def _new_user(client, admin, role="analyst", scope=None, must_change=False):
    email = f"u{uuid.uuid4().hex[:10]}@demo.yalainsight.local"
    r = client.post(f"{API}/users", headers=admin, json={"email": email, "full_name": "Test User", "role": role, "password": PW,
                                                        "plant_scope": scope, "must_change_password": must_change})
    assert r.status_code == 201, r.text
    return email, r.json()["id"]


def _login(client, email, pw=PW, otp=None):
    return client.post(f"{API}/auth/login", json={"email": email, "password": pw, "otp": otp})


def _h(r):
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_totp_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.verify(secret, "287082", at=59) and not totp.verify(secret, "287083", at=59)
    assert totp.provisioning_uri(secret, "a@b.c").startswith("otpauth://totp/YalaInsight:a%40b.c?secret=")


def test_db_url_normalisation():
    assert _db_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _db_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _db_url("postgresql+psycopg://x") == "postgresql+psycopg://x"


def test_account_lockout_and_unlock(client, admin):
    email, uid = _new_user(client, admin)
    for _ in range(5):
        assert _login(client, email, "wrong-password-1").status_code == 401
    assert _login(client, email).status_code == 423  # locked, even with the right password
    users = {u["id"]: u for u in client.get(f"{API}/users", headers=admin).json()}
    assert users[uid]["locked"] is True
    client.patch(f"{API}/users/{uid}", headers=admin, json={"unlock": True})
    assert _login(client, email).status_code == 200


def test_two_factor_flow(client, admin):
    email, uid = _new_user(client, admin)
    h = _h(_login(client, email))
    setup = client.post(f"{API}/auth/mfa/setup", headers=h).json()
    secret = setup["secret"]
    assert client.post(f"{API}/auth/mfa/enable", headers=h, json={"otp": "000000"}).status_code == 422
    code = totp.code_at(secret, int(time.time() // 30))
    assert client.post(f"{API}/auth/mfa/enable", headers=h, json={"otp": code}).json()["mfa_enabled"] is True
    r = _login(client, email)
    assert r.status_code == 401 and r.json()["detail"] == "mfa_required"
    assert _login(client, email, otp="123456").status_code == 401
    assert _login(client, email, otp=totp.code_at(secret, int(time.time() // 30))).status_code == 200
    with system_session() as db:  # secret is encrypted at rest
        stored = db.get(User, uuid.UUID(uid)).totp_secret
        assert stored and secret not in stored
    # admin reset clears 2FA and revokes sessions
    client.patch(f"{API}/users/{uid}", headers=admin, json={"reset_mfa": True})
    assert client.get(f"{API}/auth/me", headers=h).status_code == 401
    assert _login(client, email).status_code == 200


def test_password_change_revokes_other_sessions(client, admin):
    email, _ = _new_user(client, admin)
    old = _h(_login(client, email))
    r = client.post(f"{API}/auth/change-password", headers=old, json={"current_password": PW, "new_password": "Brand-New-Passw0rd"})
    assert r.status_code == 200
    assert client.get(f"{API}/plants", headers=old).status_code == 401
    assert client.get(f"{API}/plants", headers=_h(r)).status_code == 200
    assert client.post(f"{API}/auth/change-password", headers=_h(r), json={"current_password": "nope", "new_password": "Another-Passw0rd"}).status_code == 401
    new = _h(_login(client, email, "Brand-New-Passw0rd"))
    client.post(f"{API}/auth/logout-all", headers=new)
    assert client.get(f"{API}/plants", headers=new).status_code == 401


def test_forced_password_change(client, admin):
    email, uid = _new_user(client, admin, must_change=True)
    r = _login(client, email)
    assert r.json()["must_change_password"] is True
    h = _h(r)
    assert client.get(f"{API}/plants", headers=h).status_code == 403
    assert client.get(f"{API}/auth/me", headers=h).json()["user"]["must_change_password"] is True
    r2 = client.post(f"{API}/auth/change-password", headers=h, json={"current_password": PW, "new_password": "My-Own-Passw0rd"})
    assert client.get(f"{API}/plants", headers=_h(r2)).status_code == 200
    # an admin password reset makes it temporary again and signs the user out
    client.patch(f"{API}/users/{uid}", headers=admin, json={"password": "Temp-Reset-Passw0rd"})
    assert client.get(f"{API}/plants", headers=_h(r2)).status_code == 401
    assert _login(client, email, "Temp-Reset-Passw0rd").json()["must_change_password"] is True


def test_plant_scope(client, admin, plants):
    nai, ben = plants["KE-NAI-01"]["id"], plants["MA-BEN-01"]["id"]
    email, uid = _new_user(client, admin, role="plant_owner", scope=[nai])
    h = _h(_login(client, email))
    assert [p["code"] for p in client.get(f"{API}/plants", headers=h).json()] == ["KE-NAI-01"]
    assert client.get(f"{API}/plants/{ben}", headers=h).status_code == 404
    assert client.get(f"{API}/plants/{ben}/reports/performance.csv", headers=h).status_code == 404
    assert {a["plant_id"] for a in client.get(f"{API}/alerts?status=", headers=h).json()} <= {nai}
    assert {w["plant_id"] for w in client.get(f"{API}/work-orders", headers=h).json()} <= {nai}
    ben_alert = client.get(f"{API}/alerts?plant_id={ben}", headers=admin).json()[0]["id"]
    assert client.get(f"{API}/alerts/{ben_alert}", headers=h).status_code == 404
    d = client.get(f"{API}/dashboard/summary", headers=h).json()
    assert d["portfolio"]["plant_count"] == 1
    assert len(client.get(f"{API}/gis/plants", headers=h).json()["features"]) == 1
    # widening access to all plants
    client.patch(f"{API}/users/{uid}", headers=admin, json={"all_plants": True})
    assert len(client.get(f"{API}/plants", headers=h).json()) == 3
    assert client.post(f"{API}/users", headers=admin, json={"email": "x@y.zz", "full_name": "x", "role": "analyst", "password": PW,
                                                           "plant_scope": [str(uuid.uuid4())]}).status_code == 422


def test_admin_cannot_lock_themselves_out(client, admin):
    me = client.get(f"{API}/auth/me", headers=admin).json()["user"]["id"]
    assert client.patch(f"{API}/users/{me}", headers=admin, json={"is_active": False}).status_code == 422


def test_webhook_retry_backoff(client, admin):
    from app.modules.integrations.webhooks import deliver, queue_event, retry_due

    hook = client.post(f"{API}/webhooks", headers=admin, json={"url": "http://localhost:9/hook", "events": ["import.completed"]}).json()
    try:
        with system_session() as db:
            sub = db.get(WebhookSubscription, uuid.UUID(hook["id"]))
            ids = queue_event(db, sub.organization_id, "import.completed", {"test": True})
            db.commit()
        deliver(ids)
        with system_session() as db:
            d = db.get(WebhookDelivery, ids[0])
            assert d.attempts == 1 and not d.success and d.next_attempt_at > datetime.now(UTC)
            d.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        assert retry_due() >= 1
        with system_session() as db:
            d = db.get(WebhookDelivery, ids[0])
            assert d.attempts == 2 and d.next_attempt_at - datetime.now(UTC) > timedelta(minutes=3)
    finally:
        client.delete(f"{API}/webhooks/{hook['id']}", headers=admin)


def test_background_jobs_run_once(seeded):
    from app.jobs import job_status, run_job

    r = run_job("anomaly_detection", force=True)
    assert r["status"] == "ok" and r["plants"] >= 3
    assert run_job("anomaly_detection") is None  # not due again yet
    assert any(j["name"] == "anomaly_detection" and j["last_status"] == "ok" for j in job_status())


def test_security_headers_and_readiness(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "tile.openstreetmap.org" in csp
    assert r.headers["x-frame-options"] == "DENY"
    ready = client.get("/ready").json()
    assert ready["tenant_isolation_enforced"] is True and ready["db_revision"] == "0002"
    cfg = client.get(f"{API}/auth/config").json()
    assert "map_tile_url" in cfg and "secret" not in str(cfg).lower()


def test_bootstrap_creates_platform_admin(seeded, monkeypatch):
    from app import bootstrap

    email = f"ops{uuid.uuid4().hex[:6]}@example.com"
    monkeypatch.setenv("YALA_BOOTSTRAP_ADMIN_EMAIL", email)
    monkeypatch.setenv("YALA_BOOTSTRAP_ADMIN_PASSWORD", "Bootstrap-Passw0rd")
    bootstrap.main()
    bootstrap.main()  # idempotent
    with system_session() as db:
        users = db.scalars(select(User).where(User.email == email)).all()
        assert len(users) == 1 and users[0].role == "platform_admin" and users[0].must_change_password
        demo = db.scalar(select(User).where(User.email == "platform@yalainsight.local"))
        assert demo.is_active is False
        demo.is_active = True  # restore for other tests
        db.commit()
