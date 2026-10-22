# API reference (v1)

- **Base URL:** `/api/v1`
- **Machine-readable spec:** `docs/openapi.json`. A running server also serves it live at
  `/api/v1/openapi.json`, with Swagger UI at `/api/docs` and ReDoc at `/api/redoc`.
- **Versioning:** the major version is in the path. Breaking changes require `/api/v2`; additive
  changes do not.

## Authentication

| Client | How |
|---|---|
| Users | `POST /auth/login {email, password}` returns `{access_token}`. Send it as `Authorization: Bearer <token>`. Tokens last 8 h by default (`YALA_JWT_TTL_MINUTES`). |
| Machines | `X-API-Key: yala_<prefix>_<secret>`. Create keys at `POST /api-keys` (client admin; requires the Enterprise `api` module). Keys are stored as SHA-256 hashes, carry a role (`read_only`, `analyst`, `technician`, `plant_owner`) and are rate-limited per plan. |

**Errors.** JSON `{"detail": …}` with these status codes:

| Code | Meaning |
|---|---|
| 401 | Not authenticated |
| 402 | Module not in plan |
| 403 | Missing permission |
| 404 | Not found (also returned for other tenants' resources) |
| 409 | Conflict |
| 413 | Upload too large |
| 422 | Validation error |
| 429 | Rate limited (`Retry-After` header) |
| 502 | External provider error |

Every response carries an `X-Request-ID` header.

**Timestamps.** ISO-8601 with an offset. Stored and returned in UTC, as the interval start. `start`
and `end` query parameters form a half-open window [start, end). If they are omitted, the window
defaults to the latest N days of available data.

## Endpoints (summary)

| Area | Method & path | Permission / module |
|---|---|---|
| Auth | `POST /auth/signup`, `POST /auth/login`, `GET /auth/me`, `GET /auth/config`, `POST /auth/change-password`, `POST /auth/logout-all`, `POST /auth/mfa/{setup,enable,disable}` | – |
| Organisation | `GET/PATCH /org`, `GET/POST /users`, `PATCH /users/{id}`, `GET /users/directory`, `GET/POST /api-keys`, `DELETE /api-keys/{id}`, `GET /audit-logs` | org:read / users:manage / apikeys:manage / audit:read |
| Platform | `GET /platform/plans`, `GET/POST /platform/organizations`, `PATCH /platform/organizations/{id}/license` | platform admin |
| Plants | `GET/POST /plants`, `GET/PATCH/DELETE /plants/{id}`, `GET/POST /plants/{id}/equipment`, `PATCH /equipment/{id}`, `GET/POST /plants/{id}/contracts`, `GET/POST /plants/{id}/events`, `GET/POST /exchange-rates`, `GET /data-sources` | plants:read/write |
| Data | `POST /plants/{id}/imports?kind=energy\|weather\|events&dry_run=…&timezone=…` (multipart `file`), `GET /plants/{id}/imports`, `GET /plants/{id}/exports/{energy\|weather\|expected}.csv` | data:import / data:export |
| Performance | `GET /plants/{id}/performance?granularity=hour\|day\|month\|year`, `GET/POST /plants/{id}/model-runs` | performance |
| Finance | `POST /plants/{id}/financial-analysis`, `POST /finance/recovery-simulation` | finance |
| Anomalies | `POST /plants/{id}/anomaly-detection`, `GET /alerts`, `GET /alerts/stats`, `GET /alerts/{id}`, `POST /alerts/{id}/review` | anomaly |
| Maintenance | `GET/POST /work-orders`, `GET/PATCH /work-orders/{id}`, `POST /work-orders/{id}/events`, `POST /work-orders/{id}/evaluate-recovery`, `GET/POST /asset-records`, `GET /equipment/{id}/history` | maintenance |
| Carbon | `GET /plants/{id}/carbon`, `GET /carbon/portfolio`, `GET/POST /emission-factors`, `POST /verified-reductions` | carbon |
| Forecast | `GET /plants/{id}/forecast?hours=…`, `GET /plants/{id}/forecast/backtest?days=…` | forecasting |
| GIS | `GET /gis/plants` (GeoJSON), `GET /gis/plants/near?lat&lon&radius_km`, `GET /gis/plants/within?bbox=minLon,minLat,maxLon,maxLat`, `GET /gis/regions`, `GET /gis/plants/{id}/assets` | gis |
| Reports | `GET /plants/{id}/reports/{performance\|financial\|maintenance\|carbon}.{pdf\|csv}` | reporting |
| Integrations | `GET /integrations/connectors`, `POST /plants/{id}/baselines/{pvgis\|pvwatts\|solargis}`, `GET/POST /webhooks`, `DELETE /webhooks/{id}`, `POST /webhooks/{id}/test`, `GET /webhooks/deliveries` | integrations |
| Dashboard | `GET /dashboard/summary` | plants:read |
| Ops | `GET /health`, `GET /ready`, `GET /metrics` (Prometheus text) | none (restrict `/metrics` at the proxy) |

## CSV formats

The header row is required and column names are case-insensitive.

| Dataset | Required | Optional |
|---|---|---|
| `energy` | `timestamp`, one of `energy_kwh` / `energy_wh` / `energy_mwh` | `equipment` (serial number or name; empty = plant meter), `power_kw` / `power_w` / `power_mw`, `interval_minutes` (1, 5, 10, 15, 30, 60; default 60), `quality` (ok / estimated / suspect) |
| `weather` | `timestamp` | `poa_wm2`, `ghi_wm2`, `ambient_temp_c`, `module_temp_c`, `wind_ms` |
| `events` | `start`, `end`, `category` | `equipment`, `description`, `confirmed` |

- **Event categories:** outage, grid_outage, curtailment, planned_maintenance, inverter_fault,
  soiling, other.
- **Range checks:** energy ≥ 0 and ≤ 120 % of rated power × interval; POA 0–1600 W/m²; temperature
  −50 to 70/100 °C.
- **Duplicates:** repeated keys within the file are rejected. Rows that already exist are skipped and
  counted. An identical file (same SHA-256) triggers a warning.
- **Structural errors:** a missing header or required column returns 422.
- **After a weather import,** the expected-generation model re-runs automatically over the imported
  window.

## Financial analysis request

```json
POST /api/v1/plants/{id}/financial-analysis
{
  "start": "2026-09-15T00:00:00Z", "end": "2026-10-15T00:00:00Z",
  "soiling_pct": 1.5, "excess_degradation_pct": 0,
  "use_alerts_for_inverter_faults": true,
  "report_currency": "KES",
  "recovery": {"categories": ["inverter_failure", "soiling"], "recovery_rate": 0.8, "repair_cost": 4000,
               "incremental_annual_cost": 0, "days_to_effect": 14, "horizon_years": 5,
               "discount_rate": 0.08, "persistent": true, "run_rate_days": 7}
}
```

**Response fields:**

- `data_quality`, `tariff`, `model` (run id, version, uncertainty)
- `scenarios.{low,base,high}`:
  - `energy`, `losses` (per category: lost production, gross value, lost revenue, addressable)
  - `gross_lost_energy_value`, `lost_revenue`
  - `recovery`: annualised loss, recovered value, net benefit, NPV, ROI, payback, cash flows
- `sensitivity`, `assumptions`

Every figure carries `basis` and a `formula`. Methodology: `docs/CALCULATIONS.md`.

## Webhooks

Deliveries are `POST`ed with JSON `{"event", "delivery_id", "data"}` and these headers:

- `X-Yala-Event`
- `X-Yala-Delivery`
- `X-Yala-Signature: sha256=HMAC_SHA256(secret, raw_body)`

**Events:** `alert.created`, `work_order.created`, `work_order.completed`, `import.completed`.

Delivery is at least once with no automatic retry yet (see `CAPABILITIES.md`). Receivers should
deduplicate on `X-Yala-Delivery`.
