# Sample data (synthetic)

All files are **synthetic** and exist only to exercise the import pipeline. They describe a hypothetical
1,000 kWp / 850 kW plant near Nairobi (register it with these capacities to avoid range errors).

| File | Dataset (`kind`) | Notes |
|---|---|---|
| `weather_hourly_7d.csv` | `weather` | Hourly POA/GHI irradiance (W/m²), temperatures (°C), wind (m/s). Importing it runs the expected-generation model. |
| `energy_meter_hourly_7d.csv` | `energy` | Revenue-meter kWh per hour. Contains an unrecorded 4-hour outage on 2026-10-04 for the anomaly detector. |
| `events_sample.csv` | `events` | One curtailment and one planned-maintenance window (excluded from loss attribution). |
| `energy_with_errors.csv` | `energy` | Deliberately invalid rows (duplicate, negative, bad timestamp, non-numeric, out of range) — try a dry run. |

Timestamps are interval start in ISO-8601. Naive timestamps are interpreted in the plant's timezone
(or the `timezone` query parameter). See `docs/API.md` for the column specification.

Suggested flow: register plant → add an energy contract → import weather → import energy → import events
→ run anomaly detection → open the Financial tab.
