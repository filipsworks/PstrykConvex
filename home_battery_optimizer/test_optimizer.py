#!/usr/bin/env python3
"""Self-check for optimizer.py: the plan must be something the inverter can
actually do, hour by hour.

The key check replays each decision through the inverter's mode semantics
(SBU = battery covers the whole house deficit, SUB/SNU = grid charges at the
given amps, PV surplus always goes into the battery; SUB/OSO never appears) and requires the
resulting SOC to match the plan's ``soc_pct``.

Run:
    .venv/bin/python home_battery_optimizer/test_optimizer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from battery_model import (  # noqa: E402
    CHARGE_EFFICIENCY,
    DISCHARGE_EFFICIENCY,
    DUMMY_LOAD_KW,
    GRID_LIMIT_KW,
    MAX_CHARGE_CURRENT_A,
    MAX_SOC,
    MAX_TOTAL_CHARGE_CURRENT_A,
    NOMINAL_VOLTAGE,
    TOTAL_CAPACITY_WH,
)
from optimizer import OBJECTIVE_MIN_COST_PER_KWH, optimize  # noqa: E402

CAP_KWH = TOTAL_CAPACITY_WH / 1000

# A typical autumn day: cheap night, morning peak, cheap noon, evening peak.
PRICES = [1.20, 1.15, 1.14, 1.12, 1.13, 1.13, 1.16, 1.22, 1.12, 0.94, 0.81, 0.49,
          0.29, 0.29, 0.36, 0.75, 1.02, 1.26, 1.49, 1.68, 1.55, 1.41, 1.32, 1.25]
LOAD = [0.25, 0.2, 0.2, 0.2, 0.2, 0.25, 0.6, 1.5, 0.4, 0.3, 0.3, 0.3,
        0.3, 0.3, 0.3, 0.3, 0.4, 0.5, 0.8, 1.0, 0.9, 0.6, 0.4, 0.3]


def _prices(values):
    return [{"hour": h, "price": p} for h, p in enumerate(values)]


def _replay(result, deficit, surplus, soc0):
    """Recompute SOC from the chosen modes only and compare with the plan."""
    soc = soc0 * CAP_KWH
    hi = max(MAX_SOC, soc0) * CAP_KWH
    for i, d in enumerate(result["decisions"]):
        h = len(deficit) - len(result["decisions"]) + i
        # Only two modes: offgrid, or AC on and charging — never a plain bypass.
        assert (d["output_mode"], d["charger_mode"]) in (("SBU", "OSO"), ("SUB", "SNU")), d
        assert d["ac_on"] == (d["output_mode"] == "SUB") and (not d["ac_on"] or d["charge_amps"] >= 10), d
        if d["output_mode"] == "SBU":
            soc -= deficit[h] / DISCHARGE_EFFICIENCY
        grid_in = d["charge_amps"] * NOMINAL_VOLTAGE / 1000
        soc += grid_in
        pv_room = MAX_TOTAL_CHARGE_CURRENT_A * NOMINAL_VOLTAGE / 1000 - grid_in
        soc += min(CHARGE_EFFICIENCY * surplus[h], pv_room, max(0.0, hi - soc))
        assert abs(soc / CAP_KWH * 100 - d["soc_pct"]) < 0.2, (h, soc / CAP_KWH, d)
        # Grid import next to the dummy circuit must stay under the breaker.
        cap = GRID_LIMIT_KW - (DUMMY_LOAD_KW if d["price_plkwh"] < 0 else 0.0)
        grid_kw = (0 if d["output_mode"] == "SBU" else deficit[h]) + grid_in / CHARGE_EFFICIENCY
        assert grid_kw <= cap + 1e-6, (h, grid_kw, cap)


def check_no_pv():
    r = optimize(_prices(PRICES), LOAD, 0.30)
    _replay(r, LOAD, [0.0] * 24, 0.30)
    # Evening peak is covered from the battery, charged in cheap hours.
    assert all(d["output_mode"] == "SBU" for d in r["decisions"][18:21])
    assert sum(d["charge_wh"] for d in r["decisions"][11:15]) > 0


def check_pv_surplus_replaces_grid_charging():
    surplus = [0.0] * 24
    for h in range(9, 16):
        surplus[h] = 2.5
    deficit = [0.0 if s else l for l, s in zip(LOAD, surplus)]
    no_pv = optimize(_prices(PRICES), LOAD, 0.30)
    pv = optimize(_prices(PRICES), deficit, 0.30, pv_surplus_kw=surplus)
    _replay(pv, deficit, surplus, 0.30)
    assert pv["summary"]["total_pv_charge_wh"] > 10_000
    assert pv["summary"]["total_charge_wh"] < no_pv["summary"]["total_charge_wh"]
    assert pv["summary"]["total_cost_pln"] < no_pv["summary"]["total_cost_pln"]


def check_negative_prices_beyond_capacity():
    # Six negative hours used to force max charging and go infeasible.
    prices = list(PRICES)
    for h, p in zip(range(10, 16), (-0.1, -0.94, -0.88, -0.56, -0.8, -0.2)):
        prices[h] = p
    r = optimize(_prices(prices), LOAD, 0.60)
    _replay(r, LOAD, [0.0] * 24, 0.60)
    neg = [d for d in r["decisions"] if d["price_plkwh"] < 0]
    # House stays on the grid (paid to consume) and the battery charges,
    # but only as fast as the breaker allows next to the dummy load.
    assert all(d["output_mode"] == "SUB" and d["charge_amps"] > 0 for d in neg)
    assert max(d["charge_amps"] for d in neg) < MAX_CHARGE_CURRENT_A


def check_breaker_caps_charging():
    load = [2.0] * 24
    r = optimize(_prices(PRICES), load, 0.15, target_soc=0.9)
    _replay(r, load, [0.0] * 24, 0.15)
    # 5.75 kW breaker − 2 kW house leaves room for 60 A, not 80 A.
    assert max(d["charge_amps"] for d in r["decisions"]) == 60


def check_never_infeasible():
    # Late in the day the target is out of reach: best effort + flag, no error.
    r = optimize(_prices(PRICES), LOAD, 0.10, target_soc=0.9, start_hour=22)
    assert r["summary"]["target_reached"] is False and r["warnings"]
    assert r["summary"]["final_soc"] > 10
    # Negative hours at midnight, empty battery, house above what the breaker
    # leaves next to the dummy load: the house stays on the grid, flagged,
    # and every grid hour still charges (no plain bypass).
    prices = [-0.5] * 4 + PRICES[4:]
    r = optimize(_prices(prices), [2.0] * 24, 0.10)
    assert r["summary"]["grid_over_limit_kwh"] > 0 and r["warnings"]
    assert all(d["ac_on"] == (d["charge_amps"] >= 10) for d in r["decisions"])


def check_offgrid_priority():
    # PV covers the house all day: no reason to touch the grid, even at flat prices.
    r = optimize(_prices([1.0] * 24), [0.0] * 24, 0.5, pv_surplus_kw=[0.5] * 24)
    assert all(d["mode"] == "offgrid" for d in r["decisions"]), r["decisions"]


def check_joint_horizon_sees_tomorrow():
    # Cheap today, dear tomorrow, no PV: today's plan already buys for tomorrow,
    # which a day-by-day plan (today ends at MinSOC) never did.
    prices = _prices([0.5] * 24 + [2.0] * 24)
    load = LOAD + LOAD
    r = optimize(prices, load, 0.30, start_hour=10)
    _replay(r, load, [0.0] * 48, 0.30)
    today, tomorrow = r["blocks"]
    assert len(today["decisions"]) == 14 and len(tomorrow["decisions"]) == 24
    assert tomorrow["decisions"][0]["hour"] == 0
    assert today["summary"]["final_soc"] > 50 and tomorrow["summary"]["total_grid_kwh"] < 1


def check_day_targets():
    prices = _prices(PRICES + PRICES)
    r = optimize(prices, LOAD + LOAD, 0.30, day_targets={0: 0.8})
    assert r["blocks"][0]["summary"]["target_reached"] is True
    assert r["blocks"][0]["summary"]["final_soc"] >= 79.9
    assert r["blocks"][1]["summary"]["target_reached"] is None


def check_min_cost_per_kwh_still_works():
    r = optimize(_prices(PRICES), LOAD, 0.30, objective=OBJECTIVE_MIN_COST_PER_KWH)
    assert r["chosen_target_soc_pct"] is not None
    _replay(r, LOAD, [0.0] * 24, 0.30)


if __name__ == "__main__":
    check_no_pv()
    check_pv_surplus_replaces_grid_charging()
    check_negative_prices_beyond_capacity()
    check_breaker_caps_charging()
    check_never_infeasible()
    check_offgrid_priority()
    check_joint_horizon_sees_tomorrow()
    check_day_targets()
    check_min_cost_per_kwh_still_works()
    print("optimizer self-check OK")
