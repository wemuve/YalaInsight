# Implemented vs outstanding capabilities

**Status: Stage 1 MVP with production hardening (v1.1).** It is deployable to production (Render
blueprint), with:

- tenant isolation enforced in the database;
- 2FA, account lockout and session revocation;
- shared rate limits, background jobs, webhook retries, health checks and backups.

**Not yet validated in production.** Before it can be called production-proven, it still needs:

- a first live deployment;
- an external security review;
- validation against real plant records (Stage 2);
- integration with at least one live telemetry source;
- operational monitoring in that environment.

Legend: ✅ implemented and tested · 🟡 partial or interface only · ⬜ outstanding

## Stage 1: Functional MVP

| Capability | Status | Notes |
|---|:-:|---|
| Authentication, tenant separation | ✅ | JWT and API keys. RLS plus app-level checks. Isolation tests use raw SQL. Production start-up refuses an RLS-bypassing DB role. |
| Role-based permissions (6 roles) | ✅ | Matrix in `SECURITY.md`. |
| Two-factor auth, lockout, session revocation, forced password change | ✅ | TOTP with encrypted secrets; admin unlock, reset and deactivation revoke sessions. |
| Plant-level access scoping | ✅ | Per-user plant list enforced across every view and endpoint. |
| Plant registration (capacity, technology, commissioning, location, boundary) | ✅ | PostGIS geography. |
| Equipment register (serials, warranties, locations, hierarchy) | ✅ | |
| Sample datasets and CSV upload | ✅ | Synthetic 3-plant portfolio plus sample CSVs. Dry run, units, timezones, range checks, duplicate detection, SHA-256 and source tracking. |
| Expected vs actual generation | ✅ | Hourly POA/temperature model. PR, PI, availability, specific yield, variance. Hour, day, month and year views. |
| Financial loss simulator | ✅ | No-double-counting attribution; production vs revenue; contract compensation; export cap; low/base/high; run-rate recovery; NPV, ROI, payback; sensitivity; dated FX; basis labels. |
| Basic anomaly detection | ✅ | 7 transparent, versioned rules with evidence, ranking, review workflow and a precision metric. |
| Maintenance tasks | ✅ | Work orders (state machine, history, assignment, contractor), asset records, post-repair realised-recovery evaluation. |
| Carbon estimates | ✅ | Configurable factors with source, method and year; estimated kept separate from verified. Seeded factors are **illustrative samples**. |
| PostGIS plant map | ✅ | Portfolio map, site map (boundary and equipment), radius / bbox queries, regional aggregation. |
| Downloadable reports | ✅ | Performance, financial, maintenance and carbon reports in PDF and CSV, with methodology notes. |
| Forecasting | 🟡 | Climatology baseline with P10–P90 and backtest. No weather-forecast feed yet. |
| Module licensing | ✅ | Plans, per-org overrides, 402 on unlicensed modules, plant limits. No payments (by design). |
| Webhooks | ✅ | Signed deliveries with a log; automatic retries with exponential backoff (6 attempts). |
| Background jobs | ✅ | Scheduled anomaly detection and webhook retries. Advisory-locked, so each job runs once across instances. Status is visible to platform admins. |
| Shared rate limiting | ✅ | Redis-backed when configured; in-process fallback. |
| Production deployment | 🟡 | Render blueprint, release step (migrations + bootstrap), pinned dependencies, strict CSP and HSTS. **Not yet deployed**; plan names should be checked on import. |
| External baselines | 🟡 | PVGIS adapter (live call; parsing tested offline) and PVWatts adapter (needs an `NREL_API_KEY`). Solargis/GSA is interface-only pending a licence. External calls were **not exercised** from the build environment. |
| Telemetry connectors (SCADA, Modbus, inverter clouds, smart meters, CMMS) | 🟡 | Interfaces and catalogue only. CSV/API ingestion works today. |
| Audit log, structured logging, health/readiness, metrics | ✅ | |
| Docker / compose / CI | 🟡 | Files provided. CI workflow and compose **not executed** here (no Docker daemon, no CI run yet). |

## Stage 2: Real-world pilot (outstanding)

- ⬜ Integrate one real plant data source (pick the pilot's inverter or SCADA platform and implement
  its `TelemetryConnector`).
- ⬜ Validate the expected model and PR against historical plant records and an independent PR test;
  calibrate losses and uncertainty per plant.
- 🟡 Record whether alerts lead to confirmed findings. The review workflow and per-rule precision
  exist, but are not yet exercised on real data.
- 🟡 Measure maintenance recommendation usefulness and estimated vs realised recovery. Post-repair
  evaluation exists but needs longer windows and seasonal normalisation.
- 🟡 Scheduled detection is implemented. Scheduled telemetry pulls need the pilot's connector. Alert
  notifications by email or SMS need a provider to be chosen; webhooks work today.

## Stage 3: Commercial platform (outstanding)

- ⬜ Additional connectors; weather-forecast integration (licensed); day-ahead and P50/P90 energy
  yield forecasting.
- ⬜ ML anomaly detectors once enough labelled alerts exist.
- ⬜ String-level analytics, IV-curve import, thermography import.
- ⬜ SSO/OIDC, self-service password reset by email, API usage metering per plan.
- ⬜ White-label theming, independent module packaging, payment provider integration (once chosen).
- ⬜ Time-series scaling (TimescaleDB or partitioning), read replicas, multi-region deployment.
- ⬜ Contract features: deemed-energy and availability-guarantee clauses, time-of-use tariffs, hourly
  merchant prices, PPA penalty and bonus bands.
- ⬜ Seasonality-aware annualisation, and correlation of soiling with rainfall data.

## Known limitations

- Annualisation of losses assumes the trailing-window rate persists; it ignores seasonality.
- The soiling and excess-degradation shares are analyst estimates unless backed by inspection.
- Plant availability is time-based and plant-level. Partial outages appear in PI and in
  inverter-level alerts.
- Alert impacts can overlap each other. The financial analysis is the source of non-double-counted
  totals.
- Without `YALA_REDIS_URL`, rate limits are per instance.
- Detection runs inside the web instances' scheduler thread. At large plant counts, move it to a
  dedicated worker service.
- All demo data, emission factors and FX rates are synthetic or illustrative.
