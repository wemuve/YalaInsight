# Calculation methodology

This document specifies every figure YalaInsight produces. The implementations are pure functions with
unit tests:

- `backend/app/modules/performance/engine.py`
- `backend/app/modules/finance/engine.py`
- `backend/app/modules/anomaly/rules.py`
- `backend/app/modules/carbon/engine.py`
- `backend/app/modules/forecasting/engine.py`

## Figure basis labels

Every figure in the API, UI and reports carries one of these labels:

| Label | Meaning |
|---|---|
| **measured** | Metered data supplied by the plant (energy meters, inverters, sensors). |
| **modelled** | Derived from the expected-generation model or a resource model (PVGIS etc.). |
| **simulated** | Depends on user-entered scenario assumptions (recovery rate, repair cost, horizon…). |
| **verified** | Confirmed by an inspection finding, a post-repair measurement or an independent third party. |

## 1. Expected generation: `yala-poa-temperature` v1.0.0

The model runs hourly on the plant's own plane-of-array (POA) irradiance and temperature data.

```
reference_yield_kWh = P_dc[kWp] × POA[W/m²] / 1000 × Δt[h]
T_cell = T_module + 3 × POA/1000                (back-of-module sensor, Sandia ΔT = 3 °C)
       = T_ambient + (NOCT − 20)/800 × POA      (fallback)
dc_kWh = reference_yield × (1 + γ/100 × (T_cell − 25)) × (1 − L_sys/100) × (1 − d/100)^years
expected_kWh = min(dc_kWh, P_ac × Δt)          (inverter clipping)
```

- Inputs are per-plant parameters: γ, NOCT, system losses, degradation rate and commissioning date.
- Hours without irradiance have **no baseline**. They are counted as missing, never as zero.
- Every run is stored as a `model_runs` row with its parameters and version. Each
  `expected_generation` row references the run that produced it.
- The baseline uncertainty defaults to ±5 % for an on-site pyranometer. Satellite or PVGIS baselines
  should be configured at ±8–10 %. The low and high scenarios use this uncertainty.
- PVGIS and PVWatts annual or monthly estimates are stored as long-term reference baselines. They are
  not used as hourly expectations, because they describe a typical year, not the actual weather.

## 2. Performance KPIs

These KPIs are computed over intervals that have both an actual and an expected value.

| KPI | Definition |
|---|---|
| Performance ratio (PR) | Σ actual ÷ Σ reference_yield (IEC 61724-1, not temperature-corrected) |
| Performance index (PI) | Σ actual ÷ Σ expected (temperature, losses and degradation adjusted) |
| Availability | Share of daylight hours (POA ≥ 50 W/m²) where plant output > 1 % of expected. Time-based and plant-level; partial (inverter) outages show in PI, not availability. |
| Specific yield | Σ actual kWh ÷ kWp |
| Variance | Σ actual − Σ expected (kWh and %) |
| Data coverage | Compared intervals ÷ all intervals |

"Actual" is the revenue or export meter (`equipment_id IS NULL`) when one exists. Otherwise it is the
sum of the inverter meters.

## 3. Loss attribution (no double counting)

The energy shortfall is attributed hour by hour. Each kWh is attributed **exactly once**, in this
priority order:

1. **Event hours.** The whole positive shortfall of the hour goes to the highest-priority active
   plant-wide event: curtailment > grid outage > planned maintenance > plant downtime. Overlapping
   events are therefore counted once.
2. **Inverter-fault hours.** Shortfall goes to `inverter_failure`, capped at
   `expected × faulted_kWp/total_kWp × lost_share`. Any remainder joins the residual pool.
3. **Residual pool.** This is the sum of (expected − actual) over all other hours, net of
   over-performance. It is split into soiling and excess degradation, each capped at the analyst's
   estimate as a % of pool expected energy. Whatever remains is **unexplained**.

Invariant (unit-tested): Σ categories = total attributed shortfall = max(0, Σ expected − Σ actual)
over compared intervals, when no event hour has over-performance.

**Sources of event and fault windows**

- Recorded `operational_events`.
- If no event was recorded, unreviewed or confirmed anomaly alerts. Alerts marked false-positive
  are never used. The UI exposes a switch to disable alert-based attribution.
  - `PLANT_ZERO_OUTPUT` alerts count as downtime.
  - `INVERTER_OFFLINE` alerts count as a full inverter share.
  - `INVERTER_UNDERPERFORMANCE` alerts count as the measured deficit share.

**Export caps.** If the contract defines an export cap, the expected energy is limited to the
sellable amount before attribution.

## 4. Valuation: lost production vs lost revenue

```
gross_lost_energy_value = Σ attributed kWh × price(contract, valuation date)
lost_revenue            = gross value − categories compensated under the contract
```

- **Price.** The contract (`ppa`, `feed_in_tariff`, `self_consumption`, `merchant`) that covers the
  valuation date is used. Escalation is applied per full contract year.
- **Self-consumption.** Energy is valued at the avoided retail cost. This is valid only for energy
  consumed on site; the UI states this.
- **Curtailment and grid outages.** When the contract compensates them (`curtailment_compensated`),
  they count as lost *production* but zero lost *revenue*.
- **Missing contract.** If no contract exists, valuation is refused rather than guessed.

## 5. Recovery simulation

Historical losses are **sunk**. Recovery values only the *future* losses that an intervention avoids:

```
run_rate_kWh           = addressable-category loss in the final N days of the period (default N = 7)
annual_addressable_kWh = run_rate_kWh × 365 / N           (0 if the loss is marked "resolved")
annual_recovered_kWh   = annual_addressable_kWh × recovery_rate
value_y                = annual_recovered_kWh × (1 − degradation)^(y−1) × price × (1 + escalation)^(y−1)
                         (year 1 pro-rated by days until the fix is effective)
net_benefit            = Σ (value_y − incremental_annual_cost) − repair_cost
NPV                    = −repair_cost + Σ net_cash_flow_y / (1 + r)^y
ROI                    = net_benefit / (repair_cost + incremental_annual_cost × horizon)
payback                = time until cumulative net cash flow ≥ 0 (linear interpolation)
```

Using the trailing run-rate means a fault that was already repaired inside the period is **not**
projected as recoverable. For example, the INV-03 outage in the demo data is not projected.

**Scenarios**

| Scenario | Baseline | Recovery rate | Price | Repair cost | Days to effect |
|---|---|---|---|---|---|
| Low | × (1 − u) | − 0.2 | × 0.9 | × 1.25 | × 2 |
| Base | as entered | as entered | as entered | as entered | as entered |
| High | × (1 + u) | + 0.1 | × 1.1 | × 0.85 | ÷ 2 |

**Sensitivity.** Each driver (price, recovery rate, repair cost, addressable loss, days to effect) is
varied ±20 % one at a time. The output is the resulting NPV range (tornado chart).

## 6. Currency

A conversion always uses an **explicit dated rate**: the latest `exchange_rates` row dated on or
before the valuation date, direct or inverse. The rate, date and source are returned with every
converted figure. If no such rate exists, the request fails rather than assuming one. Portfolio
totals across currencies are shown only when every plant can be converted.

## 7. Post-repair (realised) recovery

When a work order is completed:

```
PI_before = actual ÷ expected during the source alert window (or the 14 days before work started)
PI_after  = actual ÷ expected during the 14 days after completion
realised_kWh_per_year = max(0, PI_after − PI_before) × expected_after × 365 / days_after
```

- For inverter work orders, the inverter's own meter is compared against its share of expected energy.
- The result is labelled **measured** and is compared with the work order's simulated estimate.
- Caveats are returned with the result: short windows, and confounding changes such as cleaning or
  seasonal effects.

## 8. Anomaly rules (v1.x)

| Rule | Logic (defaults) | Confidence |
|---|---|---|
| `PLANT_ZERO_OUTPUT` | ≥ 2 daylight h (POA ≥ 200 W/m²) with output ≤ 2 % of expected. Excludes known curtailment, grid and maintenance windows. Runs span nights. | high |
| `INVERTER_OFFLINE` | Inverter ≤ 2 % of its expected share for ≥ 2 daylight h while ≥ 1 peer ≥ 50 % of its share. | high |
| `INVERTER_UNDERPERFORMANCE` | Weekly kWh/kWp ≥ 10 % below the peer median (≥ 3 inverters). Consecutive weeks are merged. | medium/high |
| `PERSISTENT_LOW_PI` | Daily PI < 90 % on ≥ 5 of 7 days. Hours already explained by an offline inverter or a sensor fault are excluded. | medium |
| `DECLINING_PI_TREND` | 14-day sliding least-squares slope ≤ −0.3 %-pt/day with R² ≥ 0.5 (possible soiling or degradation). | low |
| `IRRADIANCE_SENSOR_FLATLINE` / `_DARK` | Identical POA for ≥ 4 h, or POA ≈ 0 while producing. | high/medium |
| `DATA_GAP` | ≥ 3 daylight h without metered data. | high |

- **Estimated impact.** Rule energy × applicable tariff.
- **Priority.** 40 × severity weight + 35 × log-scaled impact + 25 × urgency. Urgency is 1 for an
  ongoing issue and decays to 0 over 30 days.
- **Overlap.** Individual alert impacts can overlap. Only the financial analysis (section 3) gives
  non-double-counted totals.
- **Confirmation.** An alert becomes `confirmed` only when a reviewer records inspection notes.
  Per-rule precision is computed as confirmed ÷ (confirmed + false positive). This is the Stage-2
  validation metric for deciding when ML detectors are justified.

## 9. Avoided emissions

```
estimated_avoided_tCO2e = metered generation kWh × EF[kgCO2e/kWh] / 1000
```

- The factor's name, method, year and source are returned with the result. **The seeded factors are
  illustrative samples** and carry a warning until they are replaced with an authoritative dated
  source.
- Verified reductions and issued credits are stored separately (`verified_reductions`) and are never
  added to estimates.

## 10. Forecasting

The baseline forecast (`climatology-hourly` v1.0.0) works as follows:

- For each hour of the day, it takes the mean and the empirical P10/P90 of actual output over the
  previous 30 days.
- It reports history coverage and returns warnings when data is missing.
- A rolling day-ahead backtest reports MAE, nRMSE and bias.

This is a transparent benchmark. A weather-driven day-ahead forecast requires a licensed irradiance
forecast feed (outstanding).
