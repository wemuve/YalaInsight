# Deployment and operations

## Local development

**Requirements:** Python ≥ 3.12, and PostgreSQL 16 with PostGIS 3.

```bash
# database (once) — the app role must NOT be a superuser, or row-level security is bypassed
sudo -u postgres psql -c "CREATE ROLE yala LOGIN PASSWORD 'yala' NOSUPERUSER;"
sudo -u postgres psql -c "CREATE DATABASE yalainsight OWNER yala;"
sudo -u postgres psql -d yalainsight -c "CREATE EXTENSION postgis;"
# (tests) same again for yalainsight_test

cd backend
pip install -r requirements-dev.txt
alembic upgrade head
python -m app.seed --reset          # synthetic demo portfolio
uvicorn app.main:app --reload       # http://localhost:8000  (UI) · /api/docs (API)
python -m pytest -q                 # unit + integration tests (uses yalainsight_test)
```

**Demo logins.** All demo users share the password `Demo-Password-2026`:

- `admin@demo.yalainsight.local`
- `owner@…`, `analyst@…`, `tech@…`, `viewer@…`
- `admin@other.yalainsight.local` (second tenant)

The platform administrator is `platform@yalainsight.local` with password `Platform-Admin-2026`.

## Single-host container deployment

```bash
cp .env.example .env    # set strong values
docker compose up -d --build   # db (PostGIS) → migrate (alembic upgrade head) → api
# put deploy/Caddyfile (or nginx) in front for TLS; the API binds to 127.0.0.1:8000
```

`deploy/initdb/01-app-role.sh` creates the `yala_app` role with NOSUPERUSER and NOBYPASSRLS.

> The compose and Dockerfile setup was written but **not executed** in the build environment, which
> had no Docker daemon. Validate it in your environment before relying on it.

## Production recommendations

| Concern | Recommendation |
|---|---|
| Database | Managed PostgreSQL 16 + PostGIS with encrypted storage, PITR (WAL archiving) and a replica. |
| Migrations | Run `alembic upgrade head` as a one-off release step, never on every app start. Every migration must be reversible (`downgrade`). |
| App | ≥ 2 stateless instances behind a load balancer. Set `YALA_ENV=production` and a strong `YALA_JWT_SECRET`. Set `YALA_SERVE_FRONTEND=false` if the UI is served from a CDN, and configure `YALA_CORS_ORIGINS` accordingly. |
| Health checks | `/health` is the liveness check (process up). `/ready` is the readiness check (DB reachable, migration revision, PostGIS version). |
| Monitoring | Scrape `/metrics` (request counters, 5xx count, DB pool). Logs are JSON on stdout, with `request_id`, path, status and duration; ship them to your log stack. Alert on 5xx rate, `/ready` failures, webhook delivery failures and import rejection spikes. |
| Background work | Webhook delivery and detection currently run in-request or in a FastAPI background task. Move them to a job queue (e.g. RQ, Celery or Arq) and add scheduled detection per plant before scaling. |

## Backups and recovery

**Backups**

1. **Continuous.** Managed PITR, or WAL archiving (e.g. `pgBackRest` / `wal-g`) to object storage
   with encryption and ≥ 14 days of retention.
2. **Nightly logical dump.** `deploy/backup/pg_backup.sh` produces a custom-format `pg_dump`,
   optionally GPG-encrypted, kept for 14 days. Copy it off-site.

**Targets**

- RPO ≤ 5 min with PITR; 24 h with dumps only.
- RTO ≤ 2 h.

**Restore procedure (tested quarterly)**

1. Provision a new PostgreSQL 16 + PostGIS instance.
2. Recreate the non-superuser app role (`deploy/initdb/01-app-role.sh`).
3. Run `pg_restore --no-owner --role=yala_app -d yalainsight <dump>`, or perform a PITR restore to
   the chosen timestamp.
4. Run `alembic upgrade head`. This is a no-op if the restored database is current.
5. Check `/ready` and spot-check tenant isolation (`python -m pytest tests/test_tenant_isolation.py`
   against a restored staging copy).
6. Point the app at the restored database and record the incident in the audit log.
