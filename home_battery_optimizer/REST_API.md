# Home Battery Optimizer — REST API

Flask-based REST service wrapping the home battery charging optimizer. Accepts all CLI arguments as query parameters and returns JSON-only output. Served via uvicorn (ASGI) with a WSGI bridge for Flask compatibility.

## Quick Start

```bash
cd home_battery_optimizer
source ../.venv/bin/activate
pip install -r requirements.txt  # includes uvicorn, starlette, flask
python rest_service.py [--host 0.0.0.0] [--port 8000] [--debug]
```

Or directly with uvicorn:

```bash
uvicorn rest_service:asgi_app --host 0.0.0.0 --port 8000
```

| Argument | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Bind address (for direct mode) |
| `--port` | `8000` | Port to listen on (for direct mode) |
| `--debug` | *(off)* | Enable Flask debug mode (for direct mode) |

The server starts on `http://<host>:<port>`.

## Persistence (auto-restart)

The service includes a `run.sh` wrapper that automatically uses **supervisor** for crash recovery when it's installed. Supervisor restarts the process on exit, crash, or abnormal termination.

```bash
cd home_battery_optimizer

# Install supervisor (one-time):
source ../.venv/bin/activate && pip install supervisor

# Start — supervisor is used automatically if available:
./run.sh [--host 0.0.0.0] [--port 8000] [--debug]
```

**How it works:**

| Scenario | Behavior |
|---|---|
| `supervisor` installed | Runs under supervisord with `autorestart=true` (automatic crash recovery) |
| `supervisor` not installed | Falls back to direct uvicorn execution with a warning message |
| `./run.sh supervisor` | Forces supervisor mode (fails if not installed) |

The generated config (`supervisord.generated.conf`) is written to disk at startup and ignored by git.

## Endpoints

### `GET /health`

Health check endpoint.

**Response:**
```json
{ "status": "ok" }
```

---

## Prediction-vs-reality feedback loop

The service automatically records every `/optimize` run's per-hour predictions
(consumption, solar, net load, price) into a local SQLite store at
`home_battery_optimizer/data/history.db` (override the path with the
`BATTERY_HISTORY_DB` env var). A nightly `/reconcile` call pulls the
actuals for the previous day from Home Assistant history, compares them
against the recorded predictions, and exponentially smooths a learned
multiplier per `(weekday, hour)` bucket into the `adjustments` table.

Subsequent `/optimize` / `/sensitivity` calls apply those multipliers by
default — pass `use_adjustments=false` to skip the lookup and get the
raw, unadjusted result (e.g. for "what would the unadjusted optimizer do?"
or A/B comparisons).

| Endpoint | Verb | Purpose |
|---|---|---|
| `/optimize` | GET/POST | Plus: auto-records predictions, accepts `use_adjustments` |
| `/sensitivity` | GET/POST | Plus: accepts `use_adjustments` |
| `/report` | GET | Side-by-side prediction-vs-reality table for one date |
| `/reconcile` | GET/POST | Pull actuals from HA + update learned multipliers |
| `/adjustments` | GET / DELETE | Inspect or wipe the learned 7×24 multiplier grid |
| `/reconciliation_log` | GET | Recent reconciliation runs (with MAPE / bias) |

`use_adjustments` (boolean, default `true`) on `/optimize` and `/sensitivity`
skips the multiplier lookup entirely. The response always carries an
`adjustments: { "applied": bool, "buckets": int }` field so callers can
tell which mode produced the numbers.

---

### `GET /optimize` (also accepts `POST` with a JSON body)

Run the charging optimization and return JSON results.

**Connection / horizon parameters:**

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `mock` | boolean | `false` | No (unless no token) | Use sample data instead of Home Assistant API |
| `ha_url` | string | `https://ha.kompfix.pl` | No | Home Assistant base URL (without `/api`) |
| `ha_token` | string | *(none)* | Yes (if `mock=false`) | Long-lived access token for Home Assistant |
| `horizon` | enum | `available` | No | Which day's prices to optimize: `today`, `tomorrow`, or `available` |
| `days` | integer | `7` | No | Days of history to fetch for the weekday×hour consumption profile (≥7 ensures every weekday is represented) |
| `target_soc` | float | *(none)* | No | Global EOD SOC % (0–100). If omitted or `-1`, optimizer chooses freely. Beaten by `per_date_target_soc` for matching dates. |
| `objective` | enum | `min_cost` | No | Optimisation objective: `min_cost` minimises total PLN spend; `min_cost_per_kwh` minimises average PLN/kWh (sweeps EOD SOC targets internally, `target_soc` is ignored). |
| `use_adjustments` | boolean | `true` | No | When `false`, skip the learned `(weekday, hour)` multipliers from `/reconcile` history — i.e. run the optimizer on the raw learned profile without any reality-driven correction. Same flag is supported on `/sensitivity`. |

**GBB-inspired override parameters** (all optional; mirror the most useful knobs from the
GBB Optimizer manual for a non-prosument LiFePo4 + Pstryk setup):

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_soc_pct` | float | `95` | Upper SOC bound % (GBB recommends `90` as a buffer against forecast error). |
| `min_soc_pct` | float | `10` | Lower SOC bound % — depth-of-discharge floor. |
| `full_charge_days` | csv | *(empty)* | Day-of-month CSV (e.g. `1,15`). On those days `max_soc` is forced to **100%** for a balance/full-charge cycle. |
| `per_date_target_soc` | csv/JSON | *(empty)* | Per-date EOD SOC pins, e.g. `2026-05-21:80,2026-05-22:30` or `{"2026-05-21":80}`. Overrides global `target_soc` for that date. |
| `consumption_scale` | float | `1.0` | Multiplier on the auto-learned profile (`0.5` "we're away", `1.4` "lots of guests"). |
| `consumption_profile` | JSON | *(empty)* | Replacement weekday map: `{"0":[24 kW],…,"6":[24 kW]}` (`0`=Mon). Equivalent to GBB "Profile of Loads" manual entry. |
| `load_override_kw` | csv | *(empty)* | CSV of 24 (or 48) kW values — **hard replace** of the net load after all other steps. |
| `extra_loads` | csv/JSON | *(empty)* | Planned one-shot loads — additive on top of the profile. CSV `hour:kw[:duration_h]` (e.g. `14:2.0:2`) or JSON `[{"hour":14,"kw":2.0,"duration_h":2,"day":0}]`. |
| `solar_scale` | float | `1.0` | PV forecast correction factor (GBB-style auto-calibrated drift correction). |
| `solar_override_kwh` | csv | *(empty)* | CSV of kWh-per-hour values; replaces the PV forecast entirely. |
| `dry_run` | boolean | `false` | Marks the response as advisory (`advisory: true`). Numbers are unchanged; the caller should NOT send decisions to the inverter. |

> **Layering order** (applied in `_build_net_load`):
> 1. `consumption_profile` replaces the auto-learned weekday map.
> 2. `consumption_scale` multiplies the per-hour kW.
> 3. `solar_override_kwh` replaces the PV forecast, then `solar_scale` multiplies it.
> 4. Net load = `max(0, consumption − solar)`.
> 5. `extra_loads` are added on top.
> 6. `load_override_kw` hard-replaces the result if supplied.

> **Prediction inputs (live mode):** the optimizer's per-hour load is
> `max(0, consumption[weekday][hour] − solar_forecast[hour])` where
> consumption comes from a median per `(weekday, hour-of-day)` over the last
> `days` of `sensor.inverter_e50000254944442369_dessmonitor_output_power` (inverter total AC output, reported in W and scaled to kW),
> and the solar forecast is read from
> `sensor.dom_energy_production_today` / `…_tomorrow` (`wh_period` attr).
> Out-of-work days (weekends + PL public holidays scraped from
> `kalendarzswiat.pl`) are surfaced on each day's response as
> `is_out_of_work` and influence the weekday-keyed consumption bucket used.

**Response:**

```json
{
  "initial_soc_pct": 35.0,
  "horizon": "available",
  "objective": "min_cost",
  "date": "2026-05-19",
  "is_out_of_work": false,
  "is_full_charge_day": true,
  "advisory": false,
  "max_soc_pct": 100.0,
  "min_soc_pct": 10.0,
  "effective_target_soc_pct": 80.0,
  "raw_consumption_kw": [0.8, 0.75, "..."],
  "solar_forecast_kwh": [0.0, 0.0, "...", 3.4, 3.1, "..."],
  "warnings": [],
  "is_estimated": false,
  "overrides_active": {
    "consumption_scale": 1.0,
    "consumption_profile_overridden": false,
    "load_override_active": false,
    "extra_loads": [],
    "solar_scale": 1.0,
    "solar_override_active": false,
    "full_charge_days": [1, 15],
    "max_soc_pct": 95.0,
    "min_soc_pct": 10.0,
    "per_date_target_soc": {"2026-05-19": 80},
    "dry_run": false
  },
  "decisions": [
    {
      "hour": 0,
      "output_mode": "SUB",
      "charger_mode": "OSO",
      "charge_wh": 0.0,
      "discharge_wh": 0.0,
      "charge_kwh": 0.0,
      "charge_amps": 0,
      "soc_pct": 35.0,
      "grid_cost_pln": 0.6504,
      "total_active_kw": 0.8,
      "total_cost_pln": 0.6504,
      "price_plkwh": 0.813
    }
  ],
  "summary": {
    "total_charge_wh": 2080.0,
    "total_discharge_wh": 0.0,
    "net_energy_wh": 2080.0,
    "cycled_pct": 1.96,
    "total_grid_kwh": 31.758,
    "total_cost_pln": 22.29,
    "final_soc": 37.0
  }
}
```

Extra response fields (compared with the pre-overrides version):

| Field | Type | Meaning |
|---|---|---|
| `is_full_charge_day` | bool | `true` if the day matched `full_charge_days` and `max_soc` was lifted to 100%. |
| `advisory` | bool | Mirrors `dry_run` — when `true`, the caller should *not* apply the decisions to the inverter. |
| `max_soc_pct` / `min_soc_pct` | float | SOC bounds actually used for the day (after balance-day promotion, etc.). |
| `effective_target_soc_pct` | float | Per-date EOD target SOC actually used (only present when one was set). |
| `overrides_active` | object | Summary of all overrides applied to this run. |

**Mode Legend:**

| Mode Pair | Meaning |
|---|---|
| `SUB/SNU` | Loads on grid + charge battery from grid (cheap hours) |
| `SBU/OSO` | Loads on battery, no charging |
| `SUB/OSO` | Loads on grid, no charging (idle battery) |

**Examples:**

```bash
# Mock run
curl "http://localhost:8000/optimize?mock=true"

# Live data with target SOC 80%
curl "http://localhost:8000/optimize?ha_url=https://ha.example.com&ha_token=YOUR_TOKEN&target_soc=80"

# Tomorrow's prices, last 3 days of history
curl "http://localhost:8000/optimize?ha_url=https://ha.example.com&ha_token=YOUR_TOKEN&horizon=tomorrow&days=3"

# Minimise PLN/kWh (cheapest energy rate, sweep-selected SOC)
curl "http://localhost:8000/optimize?mock=true&objective=min_cost_per_kwh"

# GBB-style: 90% SOC buffer, full charge on the 1st and 15th of each month
curl "http://localhost:8000/optimize?mock=true&max_soc_pct=90&full_charge_days=1,15"

# Scheduled outing today — pin tomorrow at 80% SOC, scale loads down 30%
curl "http://localhost:8000/optimize?mock=true&consumption_scale=0.7&per_date_target_soc=2026-05-20:80"

# Doing laundry 14:00–16:00 (extra 2 kW for two hours) — advisory dry run
curl "http://localhost:8000/optimize?mock=true&extra_loads=14:2.0:2&dry_run=true"

# PV forecast over-optimistic by 20% — apply correction factor
curl "http://localhost:8000/optimize?mock=true&solar_scale=0.8"

# POST with a JSON body when overrides contain commas/braces
curl -X POST http://localhost:8000/optimize \
     -H 'Content-Type: application/json' \
     -d '{
           "mock": "true",
           "extra_loads": [
             {"hour": 14, "kw": 2.0, "duration_h": 2}
           ],
           "consumption_profile": "{\"0\":[0.8,0.7,0.7,0.7,0.7,0.8,1.0,1.2,1.3,1.4,1.5,1.6,1.5,1.4,1.5,1.6,1.8,2.0,2.2,2.5,2.0,1.5,1.2,1.0],\"6\":[0.5,0.5,0.5,0.5,0.5,0.5,0.6,0.8,1.0,1.2,1.4,1.6,1.7,1.8,1.7,1.6,1.5,1.4,1.3,1.2,1.1,1.0,0.8,0.6]}"
         }'
```

---

### `GET /sensitivity` (also accepts `POST` with a JSON body)

Run sensitivity analysis: grid cost vs target SOC across the active SOC band (`min_soc_pct` … `max_soc_pct`, step 5%). Returns a baseline (unconstrained) plus per-SOC-point deltas.

**Parameters:** Same as `/optimize` except `target_soc` is not accepted (it's the variable being swept). All GBB-style override knobs (`consumption_*`, `extra_loads`, `solar_*`, `max_soc_pct`, `min_soc_pct`, `full_charge_days`, `per_date_target_soc`, `load_override_kw`, `dry_run`) apply identically.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mock` | boolean | `false` | Use sample data |
| `ha_url` | string | `https://ha.kompfix.pl` | Home Assistant base URL |
| `ha_token` | string | *(none)* | HA token (required if `mock=false`) |
| `horizon` | enum | `available` | Which day's prices: `today`, `tomorrow`, `available` |
| `days` | integer | `7` | Days of history for load estimation |

The response includes `max_soc_pct` / `min_soc_pct` (the bounds the sweep was clamped to) and `overrides_active`, so a dashboard can show why the sweep range moved (e.g. balance day → 100%).

**Response:**

```json
{
  "initial_soc_pct": 35.0,
  "horizon": "available",
  "warnings": [],
  "baseline": {
    "cost_pln": 22.29,
    "grid_kwh": 31.758,
    "cost_per_kwh": 0.70187,
    "final_soc_pct": 10.0
  },
  "points": [
    {
      "target_soc": 0,
      "total_cost_pln": 22.29,
      "grid_kwh": 31.758,
      "cost_per_kwh": 0.70187,
      "delta_cost_pln": 0.0,
      "delta_cost_per_kwh": 0.0
    },
    {
      "target_soc": 5,
      "total_cost_pln": 22.29,
      "grid_kwh": 31.758,
      "cost_per_kwh": 0.70187,
      "delta_cost_pln": 0.0,
      "delta_cost_per_kwh": 0.0
    }
  ]
}
```

**Example:**

```bash
curl "http://localhost:8000/sensitivity?mock=true"
```

---

### `GET /report`

Side-by-side prediction-vs-reality table for one calendar date. Pulls the
latest prediction recorded for each hour (i.e. the most recent `/optimize`
call that covered the date) and joins it against the actuals stored by
the last `/reconcile` for that date.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `date` | string (`YYYY-MM-DD`) | yesterday (Warsaw) | Which day to report on |

**Response:**
```json
{
  "target_date": "2026-05-18",
  "summary": {
    "consumption_mape": 12.4,
    "solar_mape": 8.1,
    "net_load_mape": 14.2,
    "predicted_net_cost_pln": 18.62,
    "actual_net_cost_pln": 21.05,
    "cost_delta_pln": 2.43,
    "hours_with_prediction": 24,
    "hours_with_actual": 24,
    "hours_compared": 24
  },
  "rows": [
    {
      "hour": 0,
      "weekday": 0,
      "price_plkwh": 0.813,
      "predicted_consumption_kw": 0.80,
      "actual_consumption_kw": 0.92,
      "consumption_err_pct": 15.0,
      "predicted_solar_kwh": 0.0,
      "actual_solar_kwh": 0.0,
      "solar_err_pct": null,
      "predicted_net_load_kw": 0.80,
      "actual_net_load_kw": 0.92,
      "net_err_pct": 15.0
    }
  ]
}
```

`*_mape` are mean absolute percentage errors; `cost_delta_pln = actual - predicted`.

---

### `GET /reconcile` (also accepts `POST`)

Pull the actual per-hour consumption and solar values for one or more
completed days from Home Assistant history, write them into the `actuals`
table, then update the learned `(weekday, hour)` multipliers using:

```
new_mult = alpha * (actual / predicted) + (1 - alpha) * old_mult
```

with `actual / predicted` clamped to `[0.5, 2.0]` to suppress one-off
spikes, and near-zero predictions (≤ 0.05 kW / kWh) skipped to avoid
divide-by-zero blow-ups.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `date` | string (`YYYY-MM-DD`) | *(none)* | Reconcile exactly this date |
| `days_back` | int | `1` | If `date` is omitted: reconcile the last N completed days |
| `mock` | boolean | `false` | Skip the HA fetch — just reconcile against actuals already in the DB |
| `ha_url` | string | `https://ha.kompfix.pl` | HA base URL |
| `ha_token` | string | *(none)* | Required unless `mock=true` |
| `alpha` | float | `0.25` | EWMA learning rate (higher = faster adapt, more jitter) |

**Response:**
```json
{
  "alpha": 0.25,
  "results": [
    {
      "target_date": "2026-05-18",
      "hours_reconciled": 24,
      "consumption_mape": 12.4,
      "solar_mape": 8.1,
      "consumption_bias": 0.12,
      "solar_bias": -0.08,
      "updated_buckets": 24,
      "notes": "updated 24 buckets across 24 hours (alpha=0.25)"
    }
  ]
}
```

`*_bias = mean(actual/predicted) - 1` — positive means actuals exceeded
predictions on average (you're using more than the optimizer expects).

**Suggested cron** (call from your scheduler at ~03:00 daily):
```
0 3 * * * curl -X POST "http://localhost:8000/reconcile?ha_token=$HA_TOKEN"
```

---

### `GET /adjustments` and `DELETE /adjustments`

`GET` returns the full 7×24 multiplier grid (one cell per `(weekday, hour)`),
including cells that have never been updated (their multipliers default to
`1.0`).

`DELETE` wipes the table — handy when the consumption pattern has shifted
(new appliance, new schedule) and the old learning is misleading.

**Response (GET):**
```json
{
  "buckets": 48,
  "grid": [
    {
      "weekday": 0,
      "hour": 0,
      "consumption_multiplier": 1.12,
      "solar_multiplier": 1.0,
      "sample_count": 4,
      "updated_at": "2026-05-19T03:00:12+00:00"
    }
  ]
}
```

**Response (DELETE):**
```json
{ "reset": true, "rows_deleted": 168 }
```

---

### `GET /reconciliation_log`

Recent reconciliation runs, newest first. Useful for "is the cron job
actually running?" health checks.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `20` | Max rows (clamped to `1..200`) |

**Response:**
```json
{
  "limit": 20,
  "log": [
    {
      "id": 3,
      "target_date": "2026-05-18",
      "reconciled_at": "2026-05-19T03:00:12+00:00",
      "hours_reconciled": 24,
      "consumption_mape": 12.4,
      "solar_mape": 8.1,
      "consumption_bias": 0.12,
      "solar_bias": -0.08,
      "notes": "updated 24 buckets across 24 hours (alpha=0.25)"
    }
  ],
  "reconciled_dates": ["2026-05-18", "2026-05-17"]
}
```

---

## Error Responses

All errors return JSON with an `error` key and appropriate HTTP status codes:

| Status | Meaning |
|---|---|
| `400` | Missing/invalid parameters (e.g. no token when not using mock) |
| `500` | Optimization or API failure |

```json
{ "error": "Either mock=true or ha_token is required" }
```
