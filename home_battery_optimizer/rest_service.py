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
import os
import sys
import threading
import time
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Optional

# Ensure the package directory is on sys.path so imports work when run directly
sys.path.insert(0, str(Path(__file__).parent))

try:
    from zoneinfo import ZoneInfo

    WARSAW_TZ = ZoneInfo("Europe/Warsaw")
except ImportError:
    WARSAW_TZ = timezone

from api import fetch_all_data  # noqa: E402
from optimizer import OBJECTIVE_MIN_COST, OBJECTIVE_MIN_COST_PER_KWH, VALID_OBJECTIVES, optimize  # noqa: E402
from overrides import OptimizerOverrides, build_overrides_from_query  # noqa: E402

import history_db  # noqa: E402
import reconciliation  # noqa: E402

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


def _get_data(
    mock: bool,
    ha_url: str,
    ha_token: str,
    horizon: str,
    days: int,
    overrides: Optional[OptimizerOverrides] = None,
):
    """Return (prices, dummy_loads, initial_soc, aux, stderr_log).

    ``aux`` carries the extra signals folded into the net load so callers can
    expose them in API responses:
        - raw_consumption: per-hour kW from weekday×hour history.
        - solar_forecast_kwh: per-hour kWh from sensor.dom_energy_production_*.
        - out_of_work_days: dates (within the horizon) that are Sat/Sun/PL holiday.
        - horizon_dates: calendar date assigned to each 24-hour price block.

    ``overrides`` is forwarded to :func:`fetch_all_data` so the returned
    ``dummy_loads`` already reflects scale factors, profile replacement,
    extra loads, and solar correction.  Mock mode applies the same overrides
    locally so behaviour stays consistent across runs.
    """
    stderr_capture = StringIO()
    aux: dict = {
        "raw_consumption": None,
        "solar_forecast_kwh": None,
        "out_of_work_days": None,
        "horizon_dates": None,
        "pv_surplus_kw": None,
    }

    if mock:
        with redirect_stderr(stderr_capture):
            prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
            dummy_loads = _apply_overrides_to_mock(MOCK_DUMMY_LOADS, overrides)
            initial_soc = 0.35
            aux["soc_source"] = "mock"
            today = datetime.now(WARSAW_TZ).date()
            aux["raw_consumption"] = list(MOCK_DUMMY_LOADS)
            aux["solar_forecast_kwh"] = [0.0] * 24
            aux["out_of_work_days"] = []
            aux["horizon_dates"] = [today.isoformat()]
    else:
        base_url = _build_base_url(ha_url)
        with redirect_stderr(stderr_capture):
            try:
                data = fetch_all_data(
                    base_url=base_url,
                    token=ha_token,
                    days=days,
                    horizon=horizon,
                    overrides=overrides,
                )
            except Exception as e:
                stderr_capture.write(
                    f"[error] API failed ({e}), falling back to mock\n"
                )
                prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
                dummy_loads = _apply_overrides_to_mock(MOCK_DUMMY_LOADS, overrides)
                initial_soc = 0.35
                aux["soc_source"] = "mock"
                today = datetime.now(WARSAW_TZ).date()
                aux["raw_consumption"] = list(MOCK_DUMMY_LOADS)
                aux["solar_forecast_kwh"] = [0.0] * 24
                aux["out_of_work_days"] = []
                aux["horizon_dates"] = [today.isoformat()]
            else:
                prices = data["prices"]
                dummy_loads = data["dummy_loads"]
                aux["raw_consumption"] = data.get("raw_consumption")
                aux["solar_forecast_kwh"] = data.get("solar_forecast_kwh")
                aux["pv_surplus_kw"] = data.get("pv_surplus_kw")
                aux["pv_correction"] = data.get("pv_correction")
                aux["day_targets"] = data.get("day_targets")
                aux["out_of_work_days"] = data.get("out_of_work_days")
                aux["horizon_dates"] = data.get("horizon_dates")
                initial_soc = data["soc"]
                aux["soc_source"] = data["soc_source"]
                if data["soc_source"] != "bms":
                    stderr_capture.write(
                        f"[warn] JK BMS SOC unavailable — initial SOC from "
                        f"{data['soc_source']}\n"
                    )

    return prices, dummy_loads, initial_soc, aux, stderr_capture.getvalue()


def _apply_overrides_to_mock(
    base_loads: list[float], overrides: Optional[OptimizerOverrides]
) -> list[float]:
    """Mock-mode mirror of the parts of :func:`api._build_net_load` that apply
    to the net-load list (scale + extra loads + hard replacement).

    Profile replacement and solar correction are no-ops in mock mode because
    the mock data is already a fixed net-load vector.  Learned multipliers
    are applied using today's weekday for every hour of the mock block.
    """
    if overrides is None:
        return list(base_loads)
    today_wd = datetime.now(WARSAW_TZ).weekday()
    loads = [
        v * overrides.consumption_scale * overrides.consumption_adjustment_for(today_wd, i % 24)
        for i, v in enumerate(base_loads)
    ]
    for load in overrides.extra_loads:
        day_idx = load.get("day", 0)
        base = day_idx * 24
        for offset in range(load["duration_h"]):
            idx = base + load["hour"] + offset
            if 0 <= idx < len(loads):
                loads[idx] += load["kw"]
    if overrides.load_override_kw is not None:
        for i in range(min(len(loads), len(overrides.load_override_kw))):
            loads[i] = max(0.0, overrides.load_override_kw[i])
    return [round(v, 3) for v in loads]


def _has_estimated_prices(prices):
    return any(p.get("is_estimated", False) for p in prices)


def _run_horizon_optimization(
    prices,
    dummy_loads,
    initial_soc,
    day_targets_pct: Optional[dict[str, float]] = None,
    start_hour=0,
    objective=OBJECTIVE_MIN_COST,
    horizon_dates: Optional[list[str]] = None,
    overrides: Optional[OptimizerOverrides] = None,
    pv_surplus: Optional[list[float]] = None,
):
    """Optimise the whole horizon — rest of today, tomorrow, estimated hours —
    as one plan, then split it per calendar day.

    Per-day knobs are resolved from ``overrides``:
      * Full-charge days (day-of-month in ``overrides.full_charge_days``)
        get ``max_soc=1.0`` for their hours.
      * ``day_targets_pct`` ({ISO date: %}) is a floor on the SOC at the end
        of that day.
    A day with no target is automatic: the cheapest plan that keeps the SOC
    above MinSOC over the whole horizon.

    Returns:
        (list of per-day result dicts, horizon result from :func:`optimize`).
    """
    if overrides is None:
        overrides = OptimizerOverrides()

    n_days = -(-len(prices) // 24)
    if len(dummy_loads) == 24 and len(prices) > 24:  # mock: one day reused
        dummy_loads = (list(dummy_loads) * n_days)[: len(prices)]
    days = [date.fromisoformat(d) for d in (horizon_dates or [])][:n_days]
    day_max = [
        overrides.max_soc_for_date(days[b]) if b < len(days) else overrides.max_soc_pct / 100.0
        for b in range(n_days)
    ]
    estimated = [_has_estimated_prices(prices[b * 24 : (b + 1) * 24]) for b in range(n_days)]

    pins = day_targets_pct or {}
    day_targets = {b: pins[d.isoformat()] / 100.0 for b, d in enumerate(days) if d.isoformat() in pins}

    result = optimize(
        prices, dummy_loads, initial_soc, start_hour=start_hour, objective=objective,
        max_soc=[day_max[i // 24] for i in range(len(prices))],
        min_soc=overrides.min_soc(), pv_surplus_kw=pv_surplus, day_targets=day_targets,
    )

    per_day = []
    for b, block in enumerate(result["blocks"]):
        for d in block["decisions"]:
            d["charge_kwh"] = round(d["charge_wh"] / 1000 - d["discharge_wh"] / 1000, 4)
        r = {
            "decisions": block["decisions"],
            "summary": block["summary"],
            "warnings": result["warnings"] if b == 0 else [],
            "objective": objective,
            "max_soc_pct": round(day_max[b] * 100, 1),
            "min_soc_pct": result["min_soc_pct"],
            # Visible in the response so the dashboard can show "Balance day: full charge".
            "is_full_charge_day": day_max[b] >= 0.999,
        }
        if b in day_targets:
            r["effective_target_soc_pct"] = round(day_targets[b] * 100, 1)
        if estimated[b]:
            r["is_estimated"] = True
        for key in ("chosen_target_soc_pct", "chosen_cost_per_kwh"):
            if key in result:
                r[key] = result[key]
        per_day.append(r)
    return per_day, result


def _plan(responses: list[dict], initial_soc: float, day_targets_pct: dict, min_soc_pct: float) -> Optional[dict]:
    """What to do with the AC breaker and the inverter, hour by hour.

    Not a prosumer site: grid energy only flows in (house + charging), PV
    the battery can't take is curtailed (``pv_lost_kwh``), nothing is exported.
    """
    hours = [d for r in responses for d in r["decisions"]]
    if not hours:
        return None

    def end_of(d):
        return (datetime.fromisoformat(d["start"]) + timedelta(hours=1)).isoformat()

    windows: list[dict] = []
    soc_before = initial_soc * 100
    for d in hours:
        if d["ac_on"]:
            if windows and windows[-1]["end"] == d["start"]:
                w = windows[-1]
            else:
                w = {"start": d["start"], "soc_from_pct": round(soc_before, 1), "max_amps": 0,
                     "grid_kwh": 0.0, "cost_pln": 0.0, "is_estimated": False}
                windows.append(w)
            w["end"] = end_of(d)
            w["max_amps"] = max(w["max_amps"], d["charge_amps"])
            w["grid_kwh"] = round(w["grid_kwh"] + d["grid_kwh"], 3)
            w["cost_pln"] = round(w["cost_pln"] + d["grid_cost_pln"], 4)
            w["soc_to_pct"] = d["soc_pct"]
            w["is_estimated"] = w["is_estimated"] or d["is_estimated"]
        soc_before = d["soc_pct"]
    for w in windows:
        w["avg_price_plkwh"] = round(w["cost_pln"] / w["grid_kwh"], 4) if w["grid_kwh"] else None

    now = hours[0]
    change = next((d for d in hours[1:] if d["mode"] != now["mode"]), None)
    low = min(hours, key=lambda d: d["soc_pct"])
    estimated = next((d for d in hours if d["is_estimated"]), None)
    keys = ("start", "mode", "ac_on", "output_mode", "charger_mode", "charge_amps", "soc_pct", "price_plkwh")
    return {
        "generated_at": datetime.now(WARSAW_TZ).isoformat(timespec="seconds"),
        "target_soc": {d: round(v, 1) for d, v in sorted(day_targets_pct.items())} or "auto",
        "min_soc_pct": min_soc_pct,
        "now": {k: now[k] for k in keys},
        "next_change": None if change is None else {
            "at": change["start"], "mode": change["mode"], "ac_on": change["ac_on"],
            "charge_amps": change["charge_amps"],
        },
        "ac_windows": windows,
        "totals": {
            "grid_kwh": round(sum(d["grid_kwh"] for d in hours), 3),
            "cost_pln": round(sum(d["grid_cost_pln"] for d in hours), 4),
            "pv_lost_kwh": round(sum(d["pv_lost_kwh"] for d in hours), 3),
            "ac_hours": len([d for d in hours if d["ac_on"]]),
            "min_soc_pct": low["soc_pct"],
            "min_soc_at": end_of(low),
            "final_soc_pct": hours[-1]["soc_pct"],
            "horizon_end": end_of(hours[-1]),
            "estimated_prices_from": estimated["start"] if estimated else None,
        },
    }


def _run_sensitivity(
    prices, dummy_loads, initial_soc, start_hour=0,
    max_soc: float = 0.95, min_soc: float = 0.10, pv_surplus=None,
):
    """Run sensitivity analysis: unconstrained baseline + target SOC sweep.

    The sweep range is clamped to ``[min_soc, max_soc]`` (in %), matching the
    bounds enforced by the LP, so target levels outside the buffer are not
    attempted.
    """
    stderr_capture = StringIO()

    with redirect_stderr(stderr_capture):
        # Baseline (no target SOC)
        try:
            baseline_result = optimize(
                prices, dummy_loads, initial_soc, target_soc=None, start_hour=start_hour,
                max_soc=max_soc, min_soc=min_soc, pv_surplus_kw=pv_surplus,
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

        # Sensitivity points across the [min_soc, max_soc] band, step 5%.
        lo = int(round(min_soc * 100))
        hi = int(round(max_soc * 100))
        points = []
        for target_pct in range(lo, hi + 1, 5):
            target_soc = target_pct / 100.0
            try:
                result = optimize(
                    prices,
                    dummy_loads,
                    initial_soc,
                    target_soc=target_soc,
                    start_hour=start_hour,
                    max_soc=max_soc,
                    min_soc=min_soc,
                    pv_surplus_kw=pv_surplus,
                )
                summary = result["summary"]
                if not summary["target_reached"]:
                    break  # targets ascend, so every higher one is out of reach too
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
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


def _request_args() -> dict:
    """Return a single flat mapping covering both GET query params and POST JSON body.

    POST bodies are useful when overrides contain commas or JSON (e.g.
    ``extra_loads`` or ``consumption_profile``) and would otherwise be
    awkward to URL-encode.
    """
    merged: dict = {}
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            merged.update({k: v for k, v in body.items() if v is not None})
    for k, v in request.args.items():
        merged.setdefault(k, v)
    return merged


def _apply_history_adjustments(
    overrides: OptimizerOverrides, use_adjustments: bool
) -> dict:
    """Fold the reconciled per-(weekday, hour) consumption multipliers into ``overrides``.

    When ``use_adjustments`` is false the override fields stay ``None`` and
    the optimizer behaves as if no reconciliation history existed at all
    (useful for "what-if" runs, A/B comparisons, or the first call after a
    full reset).

    Returns a short summary that the caller can include in the response
    (number of buckets active + whether learning was bypassed).
    """
    if not use_adjustments:
        overrides.consumption_adjustment = None
        return {"applied": False, "buckets": 0}

    raw = history_db.get_adjustments()
    cons_map = {
        key: float(vals["consumption_multiplier"])
        for key, vals in raw.items()
        if vals.get("consumption_multiplier") is not None
    }
    overrides.consumption_adjustment = cons_map or None
    return {"applied": True, "buckets": len(raw)}


def _record_predictions_for_response(
    responses: list[dict], adjustments_applied: bool
) -> None:
    """Persist per-hour predictions from a successful /optimize response.

    The response we send back already has everything we need (one block per
    day with `date`, `decisions`, `raw_consumption_kw`, `solar_forecast_kwh`).
    We capture it row-by-row so subsequent reconciliation can join on
    (target_date, hour).

    Failures here are swallowed — the user's run already succeeded; we
    don't want a DB hiccup to bubble up as a 500.
    """
    try:
        for day in responses:
            target_date = day.get("date")
            if not target_date:
                continue
            try:
                day_obj = date.fromisoformat(target_date)
            except (TypeError, ValueError):
                continue
            weekday = day_obj.weekday()
            cons_block = day.get("raw_consumption_kw") or []
            sol_block = day.get("solar_forecast_kwh") or []
            decisions = day.get("decisions") or []
            rows = []
            for d in decisions:
                hour = d.get("hour")
                if hour is None:
                    continue
                cons = cons_block[hour] if hour < len(cons_block) else None
                sol = sol_block[hour] if hour < len(sol_block) else None
                # The optimizer-visible net load is consumption − solar
                # (clamped at 0), matching what _build_net_load produced.
                net = (
                    max(0.0, (cons or 0.0) - (sol or 0.0))
                    if cons is not None
                    else None
                )
                rows.append(
                    {
                        "target_date": target_date,
                        "hour": hour,
                        "weekday": weekday,
                        "predicted_consumption_kw": cons,
                        "predicted_solar_kwh": sol,
                        "predicted_net_load_kw": net,
                        "price_plkwh": d.get("price_plkwh"),
                        "adjustments_applied": adjustments_applied,
                    }
                )
            if rows:
                history_db.record_predictions(rows)
    except Exception as e:  # noqa: BLE001
        # Log but don't fail the user's request.
        print(f"[history] record_predictions failed: {e}", file=sys.stderr)


@app.route("/optimize", methods=["GET", "POST"])
def optimize_endpoint():
    """Run optimization and return JSON results.

    Connection parameters:
        mock            — Use sample data (true/false, default: false)
        ha_url          — Home Assistant base URL (default: https://ha.kompfix.pl)
        ha_token        — HA long-lived access token (required unless mock)
        horizon         — Which day's prices: today/tomorrow/available (default: available)
        days            — Days of load history for the profile (default: 28)

    Solver knobs:
        target_soc          — EOD SOC % (0–100) for the day the horizon names
                              (every published day for 'available'), -1 =
                              automatic for it. Other days take the dashboard
                              helpers input_number.soc_target_today/_tomorrow.
        objective           — 'min_cost' or 'min_cost_per_kwh'.

    GBB-inspired overrides (all optional):
        max_soc_pct         — MaxSOC buffer % (default 95, GBB recommends 90).
        min_soc_pct         — MinSOC % (default 10, depth-of-discharge floor).
        full_charge_days    — CSV day-of-month (e.g. "1,15") forced to 100% SOC.
        per_date_target_soc — CSV/JSON of date→%, e.g. "2026-05-21:80".
        consumption_scale   — Multiplier on the learned profile (e.g. 0.5 "away").
        consumption_profile — JSON {weekday(0=Mon)..6: [24 kW]} replacement.
        load_override_kw    — CSV of 24 (or 48) kW values: hard-replace net load.
        extra_loads         — JSON list or "hour:kw[:duration_h]" CSV — additive.
        solar_scale         — PV forecast correction factor (default 1.0).
        solar_override_kwh  — CSV of kWh-per-hour values, replaces PV forecast.
        dry_run             — true → response carries advisory=true; no semantic
                              change to numbers, but the dashboard / caller can
                              choose not to push the decisions to the inverter.

    Examples:
        GET  /optimize?mock=true&full_charge_days=1,15&max_soc_pct=90
        POST /optimize  body={"mock":"true","extra_loads":"[{\"hour\":14,\"kw\":2}]"}
    """
    args = _request_args()
    mock = _parse_bool(args.get("mock")) or False
    ha_url = args.get("ha_url", "https://ha.kompfix.pl")
    ha_token = args.get("ha_token", "")
    horizon = args.get("horizon", "available")
    days = int(args.get("days", 28))
    target_soc_raw = args.get("target_soc")

    # Validate
    if not mock and not ha_token:
        return jsonify({"error": "Either mock=true or ha_token is required"}), 400

    valid_horizons = ("today", "tomorrow", "available")
    if horizon not in valid_horizons:
        return jsonify({"error": f"horizon must be one of: {valid_horizons}"}), 400

    objective = args.get("objective", OBJECTIVE_MIN_COST)
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

    try:
        overrides = build_overrides_from_query(args)
    except ValueError as e:
        return jsonify({"error": f"Bad override: {e}"}), 400

    use_adjustments = _parse_bool(args.get("use_adjustments"))
    if use_adjustments is None:
        use_adjustments = True
    adj_meta = _apply_history_adjustments(overrides, use_adjustments)

    if not mock:
        _last_ha.update(url=ha_url, token=ha_token)  # for the nightly reconcile

    # Always plan the whole horizon from now; 'today' / 'tomorrow' only pick
    # which day of that plan is returned, so tomorrow starts from the SOC the
    # plan reaches at midnight, not from the SOC right now.
    prices, dummy_loads, initial_soc, aux, stderr_log = _get_data(
        mock, ha_url, ha_token, "available", days, overrides=overrides
    )
    start_hour = 0 if mock else datetime.now(WARSAW_TZ).hour

    # End-of-day SOC targets: the dashboard helpers (today / tomorrow), then
    # this request's target_soc for the day(s) it names (-1 clears), then an
    # explicit per_date_target_soc — so the today and tomorrow sensors, each
    # asking with its own target, still plan with both.
    pins = dict(aux.get("day_targets") or {})
    if target_soc_raw is not None:
        today = datetime.now(WARSAW_TZ).date()
        dates = aux.get("horizon_dates") or []
        named = {
            "today": [today.isoformat()],
            "tomorrow": [(today + timedelta(days=1)).isoformat()],
        }.get(horizon, [d for b, d in enumerate(dates) if not _has_estimated_prices(prices[b * 24 : (b + 1) * 24])])
        for d in named:
            if target_soc is None:
                pins.pop(d, None)
            else:
                pins[d] = target_soc * 100.0
    pins.update(overrides.per_date_target_soc)

    # One joint plan over every hour with a price (published or estimated).
    try:
        all_results, _ = _run_horizon_optimization(
            prices, dummy_loads, initial_soc, pins, start_hour, objective,
            horizon_dates=aux.get("horizon_dates"),
            overrides=overrides,
            pv_surplus=aux.get("pv_surplus_kw"),
        )
    except Exception as e:
        return jsonify({"error": f"Optimization failed: {str(e)}"}), 500

    horizon_dates = aux.get("horizon_dates") or []
    out_of_work_set = set(aux.get("out_of_work_days") or [])
    raw_consumption = aux.get("raw_consumption") or []
    solar_kwh = aux.get("solar_forecast_kwh") or []

    # Build response with day labels
    responses = []
    for i, result in enumerate(all_results):
        date_label = f"Day {i + 1}"
        block_date = horizon_dates[i] if i < len(horizon_dates) else None

        # Collect warnings from this day's optimization
        day_warnings = []
        if result.get("is_estimated"):
            day_warnings.append(
                f"Prices for {block_date} not published yet — estimated from the last known day"
            )
        if block_date:
            midnight = datetime.combine(date.fromisoformat(block_date), datetime.min.time(), WARSAW_TZ)
            for d in result["decisions"]:
                d["start"] = (midnight + timedelta(hours=d["hour"])).isoformat()
        block_raw = raw_consumption[i * 24 : (i + 1) * 24] if raw_consumption else []
        block_solar = solar_kwh[i * 24 : (i + 1) * 24] if solar_kwh else []

        responses.append(
            {
                "day_label": date_label,
                "date": block_date,
                "is_out_of_work": block_date in out_of_work_set if block_date else None,
                "objective": objective,
                "initial_soc_pct": round(result["decisions"][0]["soc_pct"], 1)
                if result["decisions"]
                else round(initial_soc * 100, 1),
                "initial_soc_source": aux.get("soc_source"),
                "pv_correction_today": aux.get("pv_correction"),
                **result,
                "raw_consumption_kw": block_raw,
                "solar_forecast_kwh": block_solar,
                "warnings": day_warnings
                + result.get("warnings", [])
                + (stderr_log.strip().splitlines() if stderr_log else []),
                "advisory": overrides.dry_run,
                "overrides_active": overrides.summary(),
                "adjustments": adj_meta,
            }
        )

    # Record predictions for later reconciliation (best-effort, never fatal).
    _record_predictions_for_response(responses, adj_meta.get("applied", False))

    plan = (
        _plan(responses, initial_soc, pins, overrides.min_soc_pct)
        if all("start" in d for r in responses for d in r["decisions"])
        else None
    )

    if horizon != "available" and not mock:  # mock is one sample day
        wanted = datetime.now(WARSAW_TZ).date() + timedelta(days=1 if horizon == "tomorrow" else 0)
        responses = [r for r in responses if r["date"] == wanted.isoformat()]
        for r in responses:
            r["day_label"] = horizon.capitalize()

    # Return single object or list depending on number of days
    if len(responses) == 1:
        return jsonify({**responses[0], "plan": plan})
    return jsonify(
        {"days": responses, "plan": plan, "advisory": overrides.dry_run, "adjustments": adj_meta}
    )


@app.route("/sensitivity", methods=["GET", "POST"])
def sensitivity_endpoint():
    """Run sensitivity analysis and return JSON.

    Accepts the same override knobs as ``/optimize`` (consumption_scale,
    consumption_profile, extra_loads, solar_scale, solar_override_kwh,
    load_override_kw, max_soc_pct, min_soc_pct, full_charge_days,
    per_date_target_soc, dry_run).  Per-date / full-charge-day overrides
    apply to the first horizon day's MaxSOC bound that the sweep itself
    is clamped to.

    Query parameters (subset of /optimize):
        mock            — Use sample data (true/false, default: false)
        ha_url          — Home Assistant base URL (default: https://ha.kompfix.pl)
        ha_token        — HA long-lived access token (required unless --mock)
        horizon         — Which day's prices: today/tomorrow/available (default: available)
        days            — Days of load history for the profile (default: 28)
    """
    args = _request_args()
    mock = _parse_bool(args.get("mock")) or False
    ha_url = args.get("ha_url", "https://ha.kompfix.pl")
    ha_token = args.get("ha_token", "")
    horizon = args.get("horizon", "available")
    days = int(args.get("days", 28))

    # Validate
    if not mock and not ha_token:
        return jsonify({"error": "Either mock=true or ha_token is required"}), 400

    valid_horizons = ("today", "tomorrow", "available")
    if horizon not in valid_horizons:
        return jsonify({"error": f"horizon must be one of: {valid_horizons}"}), 400

    try:
        overrides = build_overrides_from_query(args)
    except ValueError as e:
        return jsonify({"error": f"Bad override: {e}"}), 400

    use_adjustments = _parse_bool(args.get("use_adjustments"))
    if use_adjustments is None:
        use_adjustments = True
    adj_meta = _apply_history_adjustments(overrides, use_adjustments)

    # Sweep one day: today from now, or tomorrow from the SOC the joint plan
    # reaches at midnight (not the SOC right now).
    prices, dummy_loads, initial_soc, aux, stderr_log = _get_data(
        mock, ha_url, ha_token, "available", days, overrides=overrides
    )
    b = 1 if horizon == "tomorrow" and not mock and len(prices) > 24 else 0
    start_hour = 0 if mock or b else datetime.now(WARSAW_TZ).hour
    if b:
        try:
            per_day, _ = _run_horizon_optimization(
                prices, dummy_loads, initial_soc,
                {**(aux.get("day_targets") or {}), **overrides.per_date_target_soc},
                start_hour=datetime.now(WARSAW_TZ).hour,
                horizon_dates=aux.get("horizon_dates"), overrides=overrides,
                pv_surplus=aux.get("pv_surplus_kw"),
            )
        except Exception as e:
            return jsonify({"error": f"Optimization failed: {str(e)}"}), 500
        initial_soc = per_day[0]["summary"]["final_soc"] / 100.0
    day = slice(24 * b, 24 * b + 24)

    # Resolve per-day MaxSOC for the swept day so the sweep is
    # clamped to whatever the user chose (e.g. balance day → 100%).
    horizon_dates = (aux.get("horizon_dates") or [])[b:]
    first_day_obj = None
    if horizon_dates:
        try:
            first_day_obj = date.fromisoformat(horizon_dates[0])
        except ValueError:
            first_day_obj = None

    day_max_soc = (
        overrides.max_soc_for_date(first_day_obj)
        if first_day_obj is not None
        else overrides.max_soc_pct / 100.0
    )
    day_min_soc = overrides.min_soc()

    # Run sensitivity on the swept day's 24h of prices, loads and PV
    try:
        result, stderr_content = _run_sensitivity(
            prices[day], dummy_loads[day], initial_soc, start_hour=start_hour,
            max_soc=day_max_soc, min_soc=day_min_soc,
            pv_surplus=(aux.get("pv_surplus_kw") or [])[day] or None,
        )
    except Exception as e:
        return jsonify({"error": f"Sensitivity analysis failed: {str(e)}"}), 500

    out_of_work_set = set(aux.get("out_of_work_days") or [])
    first_date = horizon_dates[0] if horizon_dates else None

    response = {
        "initial_soc_pct": round(initial_soc * 100, 1),
        "horizon": horizon,
        "date": first_date,
        "is_out_of_work": first_date in out_of_work_set if first_date else None,
        "raw_consumption_kw": (aux.get("raw_consumption") or [])[day],
        "solar_forecast_kwh": (aux.get("solar_forecast_kwh") or [])[day],
        "warnings": stderr_log.strip().splitlines() if stderr_log else [],
        "advisory": overrides.dry_run,
        "overrides_active": overrides.summary(),
        "adjustments": adj_meta,
        "max_soc_pct": round(day_max_soc * 100, 1),
        "min_soc_pct": round(day_min_soc * 100, 1),
        **result,
    }

    return jsonify(response)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


# ── Prediction-vs-reality tracking ─────────────────────────────────────────


@app.route("/report", methods=["GET"])
def report_endpoint():
    """Return a prediction-vs-reality report for one date.

    Query params:
        date   — YYYY-MM-DD (default: yesterday in Warsaw local time)
    """
    args = _request_args()
    today_warsaw = datetime.now(WARSAW_TZ).date()
    target = args.get("date") or (today_warsaw - timedelta(days=1)).isoformat()
    try:
        date.fromisoformat(target)
    except ValueError:
        return jsonify({"error": f"Bad date '{target}', expected YYYY-MM-DD"}), 400
    return jsonify(reconciliation.build_report(target))


@app.route("/reconcile", methods=["GET", "POST"])
def reconcile_endpoint():
    """Pull actuals from HA for a given date and update learned multipliers.

    Query params:
        date            — YYYY-MM-DD to reconcile (default: yesterday)
        days_back       — Alternative: reconcile the last N completed days
                          (default: 1 when 'date' is not provided)
        mock            — true → skip HA fetch, just rerun reconciliation
                          against actuals already in the DB
        ha_url          — HA base URL (default https://ha.kompfix.pl)
        ha_token        — HA long-lived access token (required unless mock)
        alpha           — EWMA learning rate (default 0.25)

    Returns one entry per reconciled date.
    """
    args = _request_args()
    mock = _parse_bool(args.get("mock")) or False
    ha_url = args.get("ha_url", "https://ha.kompfix.pl")
    ha_token = args.get("ha_token", "")
    alpha_raw = args.get("alpha")
    alpha = float(alpha_raw) if alpha_raw is not None else reconciliation.DEFAULT_ALPHA

    if not mock and not ha_token:
        return jsonify({"error": "Either mock=true or ha_token is required"}), 400

    date_str = args.get("date")
    days_back_raw = args.get("days_back")

    today_warsaw = datetime.now(WARSAW_TZ).date()

    targets: list[date] = []
    if date_str:
        try:
            targets.append(date.fromisoformat(date_str))
        except ValueError:
            return jsonify({"error": f"Bad date '{date_str}'"}), 400
    else:
        days_back = int(days_back_raw) if days_back_raw else 1
        if days_back < 1 or days_back > 60:
            return jsonify({"error": "days_back must be 1..60"}), 400
        for off in range(1, days_back + 1):
            targets.append(today_warsaw - timedelta(days=off))

    results = []
    for d in targets:
        if not mock:
            base_url = _build_base_url(ha_url)
            try:
                reconciliation.pull_actuals_from_ha(
                    base_url=base_url, token=ha_token, target_date=d
                )
            except Exception as e:  # noqa: BLE001
                results.append(
                    {"target_date": d.isoformat(), "error": str(e)}
                )
                continue
        r = reconciliation.reconcile_date(d.isoformat(), alpha=alpha)
        results.append(
            {
                "target_date": r.target_date,
                "hours_reconciled": r.hours_reconciled,
                "consumption_mape": r.consumption_mape,
                "solar_mape": r.solar_mape,
                "consumption_bias": r.consumption_bias,
                "solar_bias": r.solar_bias,
                "updated_buckets": len(r.updated_buckets),
                "notes": r.notes,
            }
        )
    return jsonify({"alpha": alpha, "results": results})


@app.route("/adjustments", methods=["GET", "DELETE"])
def adjustments_endpoint():
    """List or reset learned multipliers.

    GET returns a 7x24 grid (weekday x hour) of consumption and solar
    multipliers plus their sample counts.  DELETE wipes the table — handy
    when the consumption pattern has shifted (new appliance, new schedule)
    and the old learning is misleading.
    """
    if request.method == "DELETE":
        rows = history_db.reset_adjustments()
        return jsonify({"reset": True, "rows_deleted": rows})

    raw = history_db.get_adjustments()
    # Materialise a dense grid so dashboards don't have to fill holes.
    grid = []
    for wd in range(7):
        for h in range(24):
            cell = raw.get((wd, h))
            grid.append(
                {
                    "weekday": wd,
                    "hour": h,
                    "consumption_multiplier": cell["consumption_multiplier"] if cell else 1.0,
                    "solar_multiplier": cell["solar_multiplier"] if cell else 1.0,
                    "sample_count": cell["sample_count"] if cell else 0,
                    "updated_at": cell["updated_at"] if cell else None,
                }
            )
    return jsonify(
        {
            "buckets": len(raw),
            "grid": grid,
        }
    )


@app.route("/reconciliation_log", methods=["GET"])
def reconciliation_log_endpoint():
    """Return recent reconciliation runs (newest first)."""
    args = _request_args()
    limit_raw = args.get("limit", "20")
    try:
        limit = max(1, min(200, int(limit_raw)))
    except (ValueError, TypeError):
        limit = 20
    return jsonify(
        {
            "limit": limit,
            "log": history_db.recent_reconciliations(limit=limit),
            "reconciled_dates": history_db.reconciled_dates(),
        }
    )


# ── Nightly reconciliation ────────────────────────────────────────────────

RECONCILE_HOUR = 3  # Warsaw local

# HA credentials of the last live /optimize call (HA polls it hourly), unless
# HA_URL / HA_TOKEN are set in the environment.  Kept in memory only.
_last_ha: dict = {}


def _seconds_until(hour: int) -> float:
    now = datetime.now(WARSAW_TZ)
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    # Through UTC: same-zone subtraction ignores a DST change in between.
    return (nxt.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def _nightly_reconcile() -> None:
    """Every night: pull yesterday's actuals from HA and update the learned multipliers."""
    while True:
        time.sleep(_seconds_until(RECONCILE_HOUR))
        url = os.environ.get("HA_URL") or _last_ha.get("url")
        token = os.environ.get("HA_TOKEN") or _last_ha.get("token")
        if not (url and token):
            print("[reconcile] skipped: no HA credentials yet (HA_URL/HA_TOKEN or a live /optimize call)",
                  file=sys.stderr)
            continue
        try:
            for r in reconciliation.auto_reconcile_recent(
                _build_base_url(url), token, days_back=1, today=datetime.now(WARSAW_TZ).date()
            ):
                print(f"[reconcile] {r.target_date}: {r.notes}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            print(f"[reconcile] failed: {e}", file=sys.stderr)


# shortcut: Flask's debug reloader imports this twice and would reconcile twice; fine for uvicorn/supervisor.
threading.Thread(target=_nightly_reconcile, name="nightly-reconcile", daemon=True).start()


# ── ASGI wrapper for uvicorn (Flask is WSGI, uvicorn expects ASGI) ────────
# a2wsgi is preferred: starlette's WSGIMiddleware is deprecated and itself
# points to a2wsgi as the replacement. starlette/uvicorn adapters are kept
# as fallbacks.

try:
    from a2wsgi import WSGIMiddleware  # noqa: E402
except ImportError:
    try:
        from starlette.middleware.wsgi import WSGIMiddleware  # noqa: E402
    except ImportError:
        try:
            from uvicorn.middleware.wsgi import WSGIMiddleware  # noqa: E402
        except ImportError:
            raise RuntimeError(
                "No WSGI->ASGI adapter found. Install one of these to run "
                "with uvicorn:\n"
                "  pip install a2wsgi\n"
                "  pip install starlette\n"
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
