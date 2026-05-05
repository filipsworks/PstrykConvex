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

### `GET /optimize`

Run the charging optimization and return JSON results.

**Query Parameters:**

| Parameter | Type | Default | Required | Description |
|---|---|---|---|---|
| `mock` | boolean | `false` | No (unless no token) | Use sample data instead of Home Assistant API |
| `ha_url` | string | `https://ha-finland.kompfix.pl` | No | Home Assistant base URL (without `/api`) |
| `ha_token` | string | *(none)* | Yes (if `mock=false`) | Long-lived access token for Home Assistant |
| `horizon` | enum | `available` | No | Which day's prices to optimize: `today`, `tomorrow`, or `available` |
| `days` | integer | `1` | No | Number of days of history to fetch for load estimation |
| `target_soc` | float | *(none)* | No | Target end-of-day SOC in percent (0–100). If omitted, optimizer chooses freely. |

**Response:**

```json
{
  "initial_soc_pct": 35.0,
  "horizon": "available",
  "warnings": [],
  "is_estimated": false,
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

**Mode Legend:**

| Mode Pair | Meaning |
|---|---|
| `SUB/SNU` | Loads on grid + charge battery from grid (cheap hours) |
| `SBU/OSO` | Loads on battery, no charging |
| `SUB/OSO` | Loads on grid, no charging (idle battery) |

**Example:**

```bash
# Mock run
curl "http://localhost:8000/optimize?mock=true"

# Live data with target SOC 80%
curl "http://localhost:8000/optimize?ha_url=https://ha.example.com&ha_token=YOUR_TOKEN&target_soc=80"

# Tomorrow's prices, last 3 days of history
curl "http://localhost:8000/optimize?ha_url=https://ha.example.com&ha_token=YOUR_TOKEN&horizon=tomorrow&days=3"
```

---

### `GET /sensitivity`

Run sensitivity analysis: grid cost vs target SOC (0–100%, step 5%). Returns a baseline (unconstrained) plus per-SOC-point deltas.

**Query Parameters:** Same as `/optimize`, except `target_soc` is not accepted (it's the variable being analyzed).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mock` | boolean | `false` | Use sample data |
| `ha_url` | string | `https://ha-finland.kompfix.pl` | Home Assistant base URL |
| `ha_token` | string | *(none)* | HA token (required if `mock=false`) |
| `horizon` | enum | `available` | Which day's prices: `today`, `tomorrow`, `available` |
| `days` | integer | `1` | Days of history for load estimation |

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

## Error Responses

All errors return JSON with an `error` key and appropriate HTTP status codes:

| Status | Meaning |
|---|---|
| `400` | Missing/invalid parameters (e.g. no token when not using mock) |
| `500` | Optimization or API failure |

```json
{ "error": "Either mock=true or ha_token is required" }
```
