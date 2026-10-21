"""Integration-test fixtures: a migrated PostGIS test database loaded with the synthetic demo portfolio.

Requires PostgreSQL + PostGIS. Set YALA_TEST_DATABASE_URL (default: local yalainsight_test as user yala).
The database user must NOT be a superuser, otherwise row-level security is bypassed and isolation tests fail.
"""
import os

os.environ["YALA_DATABASE_URL"] = os.environ.get(
    "YALA_TEST_DATABASE_URL", "postgresql+psycopg://yala:yala@localhost:5432/yalainsight_test")
os.environ.setdefault("YALA_LOGIN_RATE_LIMIT", "10000")
os.environ.setdefault("YALA_RATE_LIMIT_PER_MINUTE", "100000")

import pytest  # noqa: E402

DB_TESTS = {"test_api.py", "test_tenant_isolation.py"}


@pytest.fixture(scope="session")
def seeded():
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    from app import seed
    seed.run(days=40, do_reset=True)
    return True


@pytest.fixture(scope="session")
def client(seeded):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


def _login(client, email, pw="Demo-Password-2026"):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def admin(client):
    return _login(client, "admin@demo.yalainsight.local")


@pytest.fixture(scope="session")
def other_admin(client):
    return _login(client, "admin@other.yalainsight.local")


@pytest.fixture(scope="session")
def login(client):
    return lambda email, pw="Demo-Password-2026": _login(client, email, pw)


@pytest.fixture(scope="session")
def plants(client, admin):
    return {p["code"]: p for p in client.get("/api/v1/plants", headers=admin).json()}
