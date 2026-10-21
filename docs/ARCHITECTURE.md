# Architecture

YalaInsight is a modular monolith: one deployable FastAPI service plus PostgreSQL with PostGIS. Each
module sits behind a clear boundary and can be extracted into its own service later if load or
licensing requires it.

```
frontend/ (static SPA, no build step)  ──HTTPS──►  FastAPI  /api/v1
                                                    │
  core/        auth (JWT, API keys) · RBAC · rate limiting · audit · structured logging
  licensing    plan modules ± per-org overrides  → require_module("finance") …
  modules/
    orgs          organisation, users, API keys, platform administration
    plants        plants (PostGIS point + boundary), equipment register, contracts, events, FX rates
    ingestion     CSV validation → energy_readings / weather_readings / operational_events (+ import_batches)
    performance   expected-generation model runs, KPIs            ← pure engine.py
    finance       loss attribution, valuation, recovery scenarios ← pure engine.py
    anomaly       transparent rules → alerts (dedup keys, review workflow) ← pure rules.py
    maintenance   work orders, history, asset records, post-repair evaluation
    carbon        avoided-emission estimates vs verified reductions ← pure engine.py
    forecasting   climatology baseline + backtest                  ← pure engine.py
    gis           GeoJSON layer, ST_DWithin / bbox queries, regional aggregation
    reporting     PDF (ReportLab) & CSV reports
    integrations  connector interfaces (PVGIS, PVWatts, Solargis, SCADA…), webhooks
    dashboard     portfolio KPIs shared by dashboard / GIS
                                                    │
                                     PostgreSQL 16 + PostGIS 3.4 (RLS on every tenant table)
```

## Design rules

- **Engines are pure.** Calculation code takes plain values and has no database or HTTP access, so it
  is deterministic and unit-tested. Services assemble the inputs; routers handle authorisation and
  transport.
- **Traceability.** Results carry the following:
  - the model or engine version;
  - parameters (`model_runs.parameters`);
  - the data source (`data_sources`, with licence, coverage and retrieval timestamp);
  - the import batch (`import_batches`, with SHA-256 and error log);
  - the basis label and the formula.
- **Tenant isolation in depth.**
  1. Every query runs in a session bound to the caller's organisation.
  2. Routers also check `organization_id`.
  3. PostgreSQL RLS policies (`FORCE ROW LEVEL SECURITY`) filter every tenant table by
     `current_setting('app.org_id')`. The setting is re-applied at the start of every transaction
     (`db.py`), so it cannot leak between pooled connections.
  4. System tasks (login lookup, seeding, webhook delivery) use an explicit `system_session()` with
     `app.bypass_rls = on`.
  5. The application DB role must not be a superuser.
- **Integrate, don't replace.** Telemetry arrives by CSV or API today. Connector interfaces
  (`integrations/connectors/base.py`) define how SCADA, logger, inverter-cloud, smart-meter and CMMS
  adapters plug in. Outbound webhooks push alerts and work orders to the customer's own systems.
- **Time series.** Data is stored at native resolution (1–60 min) in UTC (interval start) and
  aggregated to hours in SQL. Unique constraints (`NULLS NOT DISTINCT` on plant / equipment / ts)
  provide idempotent re-imports. At larger scale, move `energy_readings` and `weather_readings` to
  TimescaleDB hypertables or native partitioning; the schema is compatible.

## Data model (main tables)

`organizations`, `subscription_plans`, `org_module_overrides`, `users`, `api_keys`, `audit_logs`,
`plants` (geography POINT + POLYGON), `equipment` (geography POINT), `energy_contracts`,
`exchange_rates`, `data_sources`, `import_batches`, `energy_readings`, `weather_readings`,
`operational_events`, `model_runs`, `expected_generation`, `alerts`, `work_orders`,
`work_order_events`, `asset_records`, `emission_factors` (shared or tenant), `verified_reductions`,
`webhook_subscriptions`, `webhook_deliveries`.

The schema is defined in `backend/app/models.py`. Migrations live in `backend/migrations/` (Alembic).
The initial migration also installs the RLS policies and the subscription plans.
