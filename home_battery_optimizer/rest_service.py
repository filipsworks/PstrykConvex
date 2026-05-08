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
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

# Ensure the package directory is on sys.path so imports work when run directly
sys.path.insert(0, str(Path(__file__).parent))

try:
    from zoneinfo import ZoneInfo

    WARSAW_TZ = ZoneInfo("Europe/Warsaw")
except ImportError:
    WARSAW_TZ = timezone

from api import fetch_all_data  # noqa: E402
from battery_model import voltage_to_soc  # noqa: E402
from optimizer import OBJECTIVE_MIN_COST, OBJECTIVE_MIN_COST_PER_KWH, VALID_OBJECTIVES, optimize  # noqa: E402

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


def _run_optimization(prices, dummy_loads, initial_soc, target_soc=None, start_hour=0, objective=OBJECTIVE_MIN_COST):
    """Run optimization and return (result, warnings).

    Args:
        prices: 24-hour price list.
        dummy_loads: 24-hour load profile.
        initial_soc: SOC at start_hour (0–1).
        target_soc: Target end-of-day SOC fraction (0–1), or None.
        start_hour: First hour to optimize (past hours are skipped).

    Returns:
        (result_dict, warnings_list)
    """
    warnings = []

    if _has_estimated_prices(prices):
        warnings.append(
            "Tomorrow's pricing data unavailable — using today's prices as estimation"
        )

    result = optimize(
        prices, dummy_loads, initial_soc, target_soc=target_soc,
        start_hour=start_hour, objective=objective,
    )

    # Inject charge_kwh (signed) into each decision
    for d in result["decisions"]:
        d["charge_kwh"] = round(d["charge_wh"] / 1000 - d["discharge_wh"] / 1000, 4)

    if _has_estimated_prices(prices):
        result["is_estimated"] = True

    return result, warnings


def _run_multi_day_optimization(
    prices, dummy_loads, initial_soc, target_soc=None, start_hour=0, objective=OBJECTIVE_MIN_COST
):
    """Run optimization for multiple days (e.g. today + tomorrow).

    Args:
        prices: List of 24×n price dicts (today first, then tomorrow, etc.).
        dummy_loads: Reusable 24-hour load profile.
        initial_soc: SOC at the beginning of day 1 (0–1).
        target_soc: Target end-of-day SOC fraction for each day, or None.
        start_hour: First hour to optimize on day 1 (past hours skipped).

    Returns:
        List of result dicts with a 'day_label' key prepended.
    """
    all_results = []
    n_days = len(prices) // 24
    current_soc = initial_soc

    for i in range(n_days):
        start = i * 24
        end = min(start + 24, len(prices))
        if start >= end:
            break

        day_prices = prices[start:end]
        # Pad dummy loads to match (should already be 24h)
        day_loads = (dummy_loads * ((end - start) // len(dummy_loads) + 1))[start:end]

        # First day skips past hours; subsequent days optimize full day
        day_start_hour = start_hour if i == 0 else 0

        try:
            result, warnings = _run_optimization(
                day_prices, day_loads, current_soc, target_soc, day_start_hour, objective
            )
        except Exception as e:
            raise RuntimeError(f"Optimization failed for day {i + 1}: {e}") from e

        # Propagate SOC to next day
        current_soc = result["summary"]["final_soc"] / 100.0

        all_results.append(result)

    return all_results


def _run_sensitivity(prices, dummy_loads, initial_soc, start_hour=0):
    """Run sensitivity analysis: unconstrained baseline + target SOC sweep."""
    stderr_capture = StringIO()

    with redirect_stderr(stderr_capture):
        # Baseline (no target SOC)
        try:
            baseline_result = optimize(
                prices, dummy_loads, initial_soc, target_soc=None, start_hour=start_hour
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
                    prices,
                    dummy_loads,
                    initial_soc,
                    target_soc=target_soc,
                    start_hour=start_hour,
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

    objective = request.args.get("objective", OBJECTIVE_MIN_COST)
    if objective not in VALID_OBJECTIVES:
        return jsonify({"error": f"objective must be one of: {list(VALID_OBJECTIVES)}"}), 400

    target_soc = None
    if target_soc_raw is not None:
        try:
            val = float(target_soc_raw)
            if val == -1:
                pass  # -1 means no constraint, same as omitting the param
            else:
                target_soc = val / 100.0
        except (ValueError, TypeError):
            return jsonify(
                {
                    "error": "target_soc must be a number between 0 and 100, or -1 for no constraint"
                }
            ), 400

    if target_soc is not None and not (0 <= target_soc <= 1):
        return jsonify({"error": "target_soc must be between 0 and 100"}), 400

    # Fetch data
    prices, dummy_loads, initial_soc, stderr_log = _get_data(
        mock, ha_url, ha_token, horizon, days
    )

    # Calculate start_hour: skip past hours for live runs.
    # Only "today" has past hours; "tomorrow"/"available" are fully future.
    if mock or horizon != "today":
        start_hour = 0
    else:
        now_warsaw = datetime.now(WARSAW_TZ)
        start_hour = now_warsaw.hour

    # Run optimization (handles multi-day when horizon="available")
    try:
        all_results = _run_multi_day_optimization(
            prices, dummy_loads, initial_soc, target_soc, start_hour, objective
        )
    except Exception as e:
        return jsonify({"error": f"Optimization failed: {str(e)}"}), 500

    # Build response with day labels
    responses = []
    for i, result in enumerate(all_results):
        if len(all_results) == 1 and horizon != "available":
            date_label = horizon.capitalize()
        else:
            date_label = f"Day {i + 1}"

        # Collect warnings from this day's optimization
        day_warnings = []
        if _has_estimated_prices(prices[i * 24 : min((i + 1) * 24, len(prices))]):
            day_warnings.append(
                "Tomorrow's pricing data unavailable — using today's prices as estimation"
            )

        responses.append(
            {
                "day_label": date_label,
                "objective": objective,
                "initial_soc_pct": round(result["decisions"][0]["soc_pct"], 1)
                if result["decisions"]
                else round(initial_soc * 100, 1),
                **result,
                "warnings": day_warnings + stderr_log.strip().splitlines()
                if stderr_log
                else [],
            }
        )

    # Return single object or list depending on number of days
    if len(responses) == 1:
        return jsonify(responses[0])
    return jsonify({"days": responses})


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

    # Fetch data (use first day's prices for sensitivity)
    prices, dummy_loads, initial_soc, stderr_log = _get_data(
        mock, ha_url, ha_token, horizon, days
    )

    # Calculate start_hour: skip past hours for live runs.
    # Only "today" has past hours; "tomorrow"/"available" are fully future.
    if mock or horizon != "today":
        start_hour = 0
    else:
        now_warsaw = datetime.now(WARSAW_TZ)
        start_hour = now_warsaw.hour

    # Run sensitivity (uses first 24h of prices)
    try:
        result, stderr_content = _run_sensitivity(
            prices[:24], dummy_loads, initial_soc, start_hour=start_hour
        )
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
