# YalaInsight

**Solar plant intelligence, financial loss analytics and asset lifecycle platform.**

YalaInsight is a modular, multi-tenant SaaS that sits alongside existing monitoring systems. It turns
plant data into technical, operational and financial decisions by answering five questions:

1. Is the plant performing as expected?
2. How much energy and revenue is being lost?
3. What is causing the loss?
4. What could be recovered?
5. Which intervention pays back best?

> **Status: Stage 1 functional MVP — not production-ready.** See
> [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) for implemented vs outstanding work. All bundled data
> is synthetic.

## Quick start

```bash
# PostgreSQL 16 + PostGIS; app role must not be a superuser (RLS) — see docs/DEPLOYMENT.md
cd backend
pip install -r requirements-dev.txt
alembic upgrade head
python -m app.seed --reset
uvicorn app.main:app --reload
# UI  → http://localhost:8000      login admin@demo.yalainsight.local / Demo-Password-2026
# API → http://localhost:8000/api/docs
python -m pytest -q          # 56 tests: engines, API, tenant isolation (needs yalainsight_test DB)
```

The container path is `cp .env.example .env && docker compose up -d --build`.

## What's inside

| Module | Highlights |
|---|---|
| Performance | Hourly expected-generation model (POA, temperature, losses, degradation, clipping); PR, PI, availability, specific yield; hourly to annual views. |
| Financial loss & recovery | Each lost kWh is attributed to one cause only. Lost production is kept separate from lost revenue, and contract compensation is respected. Includes low/base/high scenarios, run-rate recovery (sunk losses are not "recoverable"), NPV/ROI/payback, sensitivity and dated FX. |
| Anomaly diagnostics | 7 transparent, versioned rules with evidence and recommended actions. Alerts are never treated as confirmed faults without an inspection record. Per-rule precision supports pilot validation. |
| Maintenance & lifecycle | Work-order state machine, history, contractor handovers, service records, warranties, and post-repair realised vs estimated recovery. |
| Carbon | Avoided-emissions estimate with factor source, method and units, kept separate from verified reductions and credits. |
| Forecasting | Climatology P10/P50/P90 baseline, backtest, missing-data warnings. |
| GIS | PostGIS plant points, boundaries and equipment; portfolio map; radius, bbox and regional queries. |
| Reporting & integrations | PDF/CSV reports, CSV import/export, versioned REST API with OpenAPI, API keys, signed webhooks, PVGIS/PVWatts adapters, connector interfaces. |
| Platform | RBAC (6 roles), PostgreSQL row-level security, module licensing per plan, audit log, JSON logs, health/readiness/metrics. |

## Repository layout

```
backend/   FastAPI app (app/), Alembic migrations, tests
frontend/  static single-page UI (vendored Leaflet + Chart.js)
docs/      ARCHITECTURE · CALCULATIONS · API (+ openapi.json) · SECURITY · DEPLOYMENT · CAPABILITIES
sample_data/  synthetic CSVs for the import workflow
deploy/    DB role init, reverse proxy, backup script
```
