#!/usr/bin/env python3
"""REST API wrapper for the Home Battery Charging Optimizer.

Accepts all CLI arguments as query parameters and returns JSON-only output.

Usage:
    uvicorn rest_service:asgi_app --host 0.0.0.0 --port 8000

Or with Python directly (uses Flask dev server):
    python rest_service.py [--host 0.0.0.0] [--port 8000] [--debug]

Endpoints:
    GET /optimize   — Run optimization for a single day
    GET /sensitivity — Run sensitivity analysis (grid cost vs target SOC)
    GET /health     — Health check
"""

import json
import sys
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

# Ensure the package directory is on sys.path so imports work when run directly
sys.path.insert(0, str(Path(__file__).parent))

from api import fetch_all_data  # noqa: E402
from battery_model import voltage_to_soc  # noqa: E402
from optimizer import optimize  # noqa: E402

# ── Mock data (fallback when API unavailable) ─────────────────────────────

MOCK_PRICES = [
    {"hour": 0, "price": 0.813, "is_cheap": False, "is_expensive": False},
    {"hour": 1, "price": 0.809, "is_cheap": False, "is_expensive": False},
    {"hour": 2, "price": 0.802, "is_cheap": False, "is_expensive": False},
    {"hour": 3, "price": 0.797, "is_cheap": False, "is_expensive": False},
    {"hour": 4, "price": 0.815, "is_cheap": False, "is_expensive": False},
    {"hour": 5, "price": 0.856, "is_cheap": False, "is_expensive": False},
    {"hour": 6, "price": 1.333, "is_cheap": False, "is_expensive": True},
    {"hour": 7, "price": 1.321, "is_cheap": False, "is_expensive": True},
    {"hour": 8, "price": 1.236, "is_cheap": False, "is_expensive": False},
    {"hour": 9, "price": 0.980, "is_cheap": False, "is_expensive": False},
    {"hour": 10, "price": 0.682, "is_cheap": True, "is_expensive": False},
    {"hour": 11, "price": 0.664, "is_cheap": True, "is_expensive": False},
    {"hour": 12, "price": 0.641, "is_cheap": True, "is_expensive": False},
    {"hour": 13, "price": 0.658, "is_cheap": True, "is_expensive": False},
    {"hour": 14, "price": 0.674, "is_cheap": True, "is_expensive": False},
    {"hour": 15, "price": 0.261, "is_cheap": True, "is_expensive": False},
    {"hour": 16, "price": 0.404, "is_cheap": True, "is_expensive": False},
    {"hour": 17, "price": 1.135, "is_cheap": False, "is_expensive": False},
    {"hour": 18, "price": 1.284, "is_cheap": False, "is_expensive": False},
    {"hour": 19, "price": 1.411, "is_cheap": False, "is_expensive": True},
    {"hour": 20, "price": 1.568, "is_cheap": False, "is_expensive": True},
    {"hour": 21, "price": 1.386, "is_cheap": False, "is_expensive": True},
    {"hour": 22, "price": 0.892, "is_cheap": False, "is_expensive": False},
    {"hour": 23, "price": 0.839, "is_cheap": False, "is_expensive": False},
]

MOCK_DUMMY_LOADS = [
    0.80,
    0.75,
    0.72,
    0.70,
    0.72,
    0.75,
    0.90,
    1.00,
    1.20,
    1.30,
    1.50,
    1.60,
    1.50,
    1.40,
    1.50,
    1.60,
    1.80,
    2.00,
    2.20,
    2.50,
    2.00,
    1.50,
    1.20,
    1.00,
]


# ── Helpers ────────────────────────────────────────────────────────────────


def _build_base_url(ha_url: str) -> str:
    url = ha_url.rstrip("/")
    if not url.endswith("/api"):
        url += "/api"
    return url


def _get_data(mock: bool, ha_url: str, ha_token: str, horizon: str, days: int):
    """Return (prices, dummy_loads, initial_soc)."""
    stderr_capture = StringIO()

    if mock:
        with redirect_stderr(stderr_capture):
            prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
            dummy_loads = MOCK_DUMMY_LOADS
            initial_soc = 0.35
    else:
        base_url = _build_base_url(ha_url)
        with redirect_stderr(stderr_capture):
            try:
                data = fetch_all_data(
                    base_url=base_url,
                    token=ha_token,
                    days=days,
                    horizon=horizon,
                )
            except Exception as e:
                stderr_capture.write(
                    f"[error] API failed ({e}), falling back to mock\n"
                )
                prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
                dummy_loads = MOCK_DUMMY_LOADS
                initial_soc = 0.35
            else:
                prices = data["prices"]
                dummy_loads = data["dummy_loads"]
                voltage = data.get("voltage")
                if voltage is not None:
                    initial_soc = voltage_to_soc(voltage)
                else:
                    initial_soc = 0.5

    return prices, dummy_loads, initial_soc, stderr_capture.getvalue()


def _has_estimated_prices(prices):
    return any(p.get("is_estimated", False) for p in prices)


def _run_optimization(prices, dummy_loads, initial_soc, target_soc=None):
    """Run optimization and return (result, warnings)."""
    warnings = []

    if _has_estimated_prices(prices):
        warnings.append(
            "Tomorrow's pricing data unavailable — using today's prices as estimation"
        )

    result = optimize(prices, dummy_loads, initial_soc, target_soc=target_soc)

    # Inject charge_kwh (signed) into each decision
    for d in result["decisions"]:
        d["charge_kwh"] = round(d["charge_wh"] / 1000 - d["discharge_wh"] / 1000, 4)

    if _has_estimated_prices(prices):
        result["is_estimated"] = True

    return result, warnings


def _run_sensitivity(prices, dummy_loads, initial_soc):
    """Run sensitivity analysis: unconstrained baseline + target SOC sweep."""
    stderr_capture = StringIO()

    with redirect_stderr(stderr_capture):
        # Baseline (no target SOC)
        try:
            baseline_result = optimize(
                prices, dummy_loads, initial_soc, target_soc=None
            )
            baseline_cost_pln = baseline_result["summary"]["total_cost_pln"]
            baseline_grid_kwh = baseline_result["summary"]["total_grid_kwh"]
            baseline_cost_per_kwh = (
                baseline_cost_pln / max(baseline_grid_kwh, 0.001)
                if baseline_grid_kwh > 0
                else 0
            )
            last_soc_pct = baseline_result["summary"]["final_soc"]
        except Exception as e:
            stderr_capture.write(f"[warn] Baseline optimization failed: {e}\n")
            baseline_cost_pln = 0
            baseline_grid_kwh = 1
            baseline_cost_per_kwh = 0
            last_soc_pct = None

        # Sensitivity points (0–100% SOC, step 5%)
        points = []
        for target_pct in range(0, 105, 5):
            target_soc = target_pct / 100.0
            try:
                result = optimize(
                    prices, dummy_loads, initial_soc, target_soc=target_soc
                )
                summary = result["summary"]
                total_cost_pln = summary["total_cost_pln"]
                grid_kwh = summary["total_grid_kwh"]
                cost_per_kwh = (
                    total_cost_pln / max(grid_kwh, 0.001) if grid_kwh > 0 else 0
                )

                points.append(
                    {
                        "target_soc": target_pct,
                        "total_cost_pln": round(total_cost_pln, 4),
                        "grid_kwh": round(grid_kwh, 3),
                        "cost_per_kwh": round(cost_per_kwh, 6),
                        "delta_cost_pln": round(total_cost_pln - baseline_cost_pln, 4),
                        "delta_cost_per_kwh": round(
                            cost_per_kwh - baseline_cost_per_kwh, 6
                        ),
                    }
                )
            except Exception as e:
                stderr_capture.write(f"[warn] Target SOC {target_pct}% failed: {e}\n")

    return {
        "baseline": {
            "cost_pln": baseline_cost_pln,
            "grid_kwh": baseline_grid_kwh,
            "cost_per_kwh": round(baseline_cost_per_kwh, 6),
            "final_soc_pct": last_soc_pct,
        },
        "points": points,
    }, stderr_capture.getvalue()


# ── Flask app ──────────────────────────────────────────────────────────────

from flask import Flask, jsonify, request  # noqa: E402

app = Flask(__name__)


def _parse_bool(value):
    """Parse a query-string boolean."""
    if value is None:
        return None
    return value.lower() in ("true", "1", "yes")


@app.route("/optimize", methods=["GET"])
def optimize_endpoint():
    """Run optimization and return JSON results.

    Query parameters:
        mock            — Use sample data (true/false, default: false)
        ha_url          — Home Assistant base URL (default: https://ha-finland.kompfix.pl)
        ha_token        — HA long-lived access token (required unless --mock)
        horizon         — Which day's prices: today/tomorrow/available (default: available)
        days            — Days of history to fetch (default: 1)
        target_soc      — Target end-of-day SOC in percent 0–100 (optional)

    Example:
        GET /optimize?mock=true
        GET /optimize?ha_url=https://ha.example.com&ha_token=MY_TOKEN&horizon=tomorrow&target_soc=80
    """
    mock = _parse_bool(request.args.get("mock")) or False
    ha_url = request.args.get("ha_url", "https://ha-finland.kompfix.pl")
    ha_token = request.args.get("ha_token", "")
    horizon = request.args.get("horizon", "available")
    days = int(request.args.get("days", 1))
    target_soc_raw = request.args.get("target_soc")

    # Validate
    if not mock and not ha_token:
        return jsonify({"error": "Either mock=true or ha_token is required"}), 400

    valid_horizons = ("today", "tomorrow", "available")
    if horizon not in valid_horizons:
        return jsonify({"error": f"horizon must be one of: {valid_horizons}"}), 400

    target_soc = None
    if target_soc_raw is not None:
        try:
            target_soc = float(target_soc_raw) / 100.0
        except (ValueError, TypeError):
            return jsonify(
                {"error": "target_soc must be a number between 0 and 100"}
            ), 400

    if target_soc is not None and not (0 <= target_soc <= 1):
        return jsonify({"error": "target_soc must be between 0 and 100"}), 400

    # Fetch data
    prices, dummy_loads, initial_soc, stderr_log = _get_data(
        mock, ha_url, ha_token, horizon, days
    )

    # Run optimization
    try:
        result, warnings = _run_optimization(
            prices, dummy_loads, initial_soc, target_soc
        )
    except Exception as e:
        return jsonify({"error": f"Optimization failed: {str(e)}"}), 500

    response = {
        "initial_soc_pct": round(initial_soc * 100, 1),
        "horizon": horizon,
        "warnings": warnings + stderr_log.strip().splitlines() if stderr_log else [],
        **result,
    }

    return jsonify(response)


@app.route("/sensitivity", methods=["GET"])
def sensitivity_endpoint():
    """Run sensitivity analysis and return JSON.

    Query parameters:
        mock            — Use sample data (true/false, default: false)
        ha_url          — Home Assistant base URL (default: https://ha-finland.kompfix.pl)
        ha_token        — HA long-lived access token (required unless --mock)
        horizon         — Which day's prices: today/tomorrow/available (default: available)
        days            — Days of history to fetch (default: 1)

    Example:
        GET /sensitivity?mock=true
    """
    mock = _parse_bool(request.args.get("mock")) or False
    ha_url = request.args.get("ha_url", "https://ha-finland.kompfix.pl")
    ha_token = request.args.get("ha_token", "")
    horizon = request.args.get("horizon", "available")
    days = int(request.args.get("days", 1))

    # Validate
    if not mock and not ha_token:
        return jsonify({"error": "Either mock=true or ha_token is required"}), 400

    valid_horizons = ("today", "tomorrow", "available")
    if horizon not in valid_horizons:
        return jsonify({"error": f"horizon must be one of: {valid_horizons}"}), 400

    # Fetch data
    prices, dummy_loads, initial_soc, stderr_log = _get_data(
        mock, ha_url, ha_token, horizon, days
    )

    # Run sensitivity
    try:
        result, stderr_content = _run_sensitivity(prices, dummy_loads, initial_soc)
    except Exception as e:
        return jsonify({"error": f"Sensitivity analysis failed: {str(e)}"}), 500

    response = {
        "initial_soc_pct": round(initial_soc * 100, 1),
        "horizon": horizon,
        "warnings": stderr_log.strip().splitlines() if stderr_log else [],
        **result,
    }

    return jsonify(response)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


# ── ASGI wrapper for uvicorn (Flask is WSGI, uvicorn expects ASGI) ────────

try:
    from starlette.middleware.wsgi import WSGIMiddleware  # noqa: E402
except ImportError:
    try:
        from uvicorn.middleware.wsgi import WSGIMiddleware  # noqa: E402
    except ImportError:
        raise RuntimeError(
            "Neither 'starlette' nor 'uvicorn[standard]' is installed. "
            "Install one of these to run with uvicorn:\n"
            "  pip install starlette\n"
            "or\n"
            "  pip install uvicorn[standard]\n"
            "\n"
            "Alternatively, use Flask's built-in server:\n"
            "  python rest_service.py [--host 0.0.0.0] [--port 8000]"
        )

asgi_app = WSGIMiddleware(app)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Home Battery Optimizer REST API")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)"
    )
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    app.run(host=args.host, port=args.port, debug=args.debug)
