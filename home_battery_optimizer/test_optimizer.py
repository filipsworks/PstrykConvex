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


def _replay(result, deficit, surplus, soc0, cheapest_first=True, floors=None):
    """Recompute SOC from the chosen modes only and compare with the plan.

    ``floors`` is {hour index: SOC fraction} for SOC targets inside the plan.
    """
    soc = soc0 * CAP_KWH
    hi = max(MAX_SOC, soc0) * CAP_KWH
    rows = []  # (hour, price, amps, SOC before, SOC after in kWh, charges by choice)
    for i, d in enumerate(result["decisions"]):
        h = len(deficit) - len(result["decisions"]) + i
        before = soc
        by_choice = d["ac_on"] and soc - deficit[h] / DISCHARGE_EFFICIENCY - 0.10 * CAP_KWH > 1e-3
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
        rows.append((h, d["price_plkwh"], d["charge_amps"], before, soc, by_choice))
    # Every cheaper hour — charging or not — runs at its cap (80 A or what the
    # breaker leaves) when a dearer hour charges: an earlier cheaper hour
    # whatever the reason (also a reserve-forced charge: the earlier hour
    # could have bought that energy), a later one when the dearer hour
    # charges by choice, i.e. offgrid there would have kept the 10 % reserve.
    # Exempt: a cheaper hour with no room for a whole 10 A step before or
    # after it; a later cheaper hour when the battery comes within one step
    # of its floor first; an earlier one when it comes within one step of its
    # ceiling in between.
    step = NOMINAL_VOLTAGE / 1000 * 10
    tol = 1e-3
    near_top = hi - step - tol
    for h_dear, p_dear, amps_dear, _, _, by_choice in rows:
        for h, p, amps, before, after, _ in rows:
            binds = amps_dear > 0 if h < h_dear else by_choice
            if not (cheapest_first and binds and p < p_dear):
                continue
            between = [r for r in rows if min(h, h_dear) <= r[0] < max(h, h_dear)]
            if h > h_dear:
                exempt = any(
                    r[4] <= max(0.10, (floors or {}).get(r[0], 0.0)) * CAP_KWH + step + tol for r in between
                )
            else:
                exempt = any(r[4] >= near_top for r in between)
            exempt = exempt or before >= near_top or after >= near_top
            cap = GRID_LIMIT_KW - (DUMMY_LOAD_KW if p < 0 else 0.0) - deficit[h]
            full = min(MAX_CHARGE_CURRENT_A, int(cap / (step / CHARGE_EFFICIENCY) + 1e-9) * 10)
            assert exempt or amps >= full, (h, p, amps, full, "dearer hour charges", h_dear, p_dear)


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
    # 12 h at the breaker's 60 A fall just short of 90 %: the target is
    # reached and the breaker is never passed.  The cheapest-hour-first rule
    # holds unless it is bent or the MILP runs out of time (both flagged).
    assert r["summary"]["target_reached"], r["summary"]
    _replay(r, load, [0.0] * 24, 0.15, cheapest_first=not r["warnings"])
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


def check_cheapest_hour_full_first():
    # 2026-10-20: a 50 % end-of-day target used to be met with 10 A at 13:00
    # (1.16 zł) next to 70 A at 23:00 (0.98 zł); the cheap hour goes to 80 A first.
    prices = [1.0] * 13 + [1.157, 1.201, 1.693, 1.796, 1.862, 1.848, 1.698, 1.599, 1.445, 1.067, 0.98]
    load = [0.0] * 13 + [0.10, 0.12, 0.44, 0.47, 0.21, 0.24, 0.29, 0.37, 0.40, 0.39, 0.37]
    r = optimize(_prices(prices), load, 0.44, start_hour=13, target_soc=0.5)
    _replay(r, load, [0.0] * 24, 0.44)
    assert r["decisions"][-1]["charge_amps"] == 80 and r["summary"]["target_reached"], r["decisions"]


def check_cheaper_hour_left_off():
    # 2026-11-12, price shield: a flat 0.871 night, 13:00 at 0.855, 23:00 at
    # 0.838.  A 69 % target was met with 10 A in six 0.871 hours while 13:00
    # stayed offgrid — a cheaper hour that does not charge at all dodged the
    # rule.  Plan made at 23:00 for the rest of today, tomorrow and 11
    # estimated hours.
    day = [0.87084] * 6 + [1.292853] * 4 + [1.2880253, 1.2589481, 1.2361931, 0.8549546, 0.87084] \
        + [1.292853] * 7 + [0.87084, 0.8383127]
    load = [0.211, 0.17, 0.13, 0.138, 0.162, 0.122, 0.14, 0.104, 0.072, 0.128, 0, 0, 0, 0.016, 0.172,
            0.449, 0.399, 0.398, 0.404, 0.682, 1.016, 0.466, 0.46, 0.352]
    pv = [0.0] * 10 + [0.274, 0.366, 0.595] + [0.0] * 11
    prices = _prices([0.87084] * 24 + day + day[:11])
    deficit = [0.189] * 24 + load + load[:11]
    surplus = [0.0] * 24 + pv + pv[:11]

    r = optimize(prices, deficit, 0.46, start_hour=23, pv_surplus_kw=surplus, day_targets={1: 0.69})
    _replay(r, deficit, surplus, 0.46, floors={47: 0.69})
    tomorrow = r["blocks"][1]
    amps = [d["charge_amps"] for d in tomorrow["decisions"]]
    assert amps[13] == 80 and amps[23] == 80 and tomorrow["summary"]["target_reached"], amps
    assert tomorrow["summary"]["ac_hours"] <= 3, amps

    # The rule must not push buying from the cheap night into the dear
    # morning: a low battery charges at 0.871 before 13:00 comes.
    r = optimize(prices, deficit, 0.17, start_hour=23, pv_surplus_kw=surplus)
    _replay(r, deficit, surplus, 0.17)
    dear_kwh = sum(d["grid_kwh"] for d in r["decisions"] if d["price_plkwh"] > 1.0)
    assert dear_kwh == 0 and not r["warnings"], (dear_kwh, r["warnings"])


def check_forced_hour_is_no_excuse():
    # 2026-11-13 09:00, battery at 24 %, same prices; tomorrow estimated.
    # The plan ran the battery down to the reserve by 22:00 (0.871) and bought
    # there — 10 A for a 31 % target, 80 A with 45 % wanted tomorrow — while
    # 13:00 (0.855) had room: a reserve-forced hour was exempt from the rule.
    day = [0.87084] * 6 + [1.292853] * 4 + [1.2880253, 1.2589481, 1.2361931, 0.8549546, 0.87084] \
        + [1.292853] * 7 + [0.87084, 0.8383127]
    deficit = [0.0] * 9 + [0.031, 0, 0, 0, 0, 0.315, 0.448, 0.399, 0.398, 0.404, 0.682, 1.016, 0.466, 0.46, 0.35] \
        + [0.348, 0.304, 0.292, 0.272, 0.245, 0.281, 0.581, 0.351, 0, 0, 0, 0, 0, 0, 0,
           0.501, 0.463, 0.356, 0.397, 0.477, 0.45, 0.458, 0.324, 0.286]
    surplus = [0.0] * 10 + [0.468, 0.017, 0.492, 0.127] + [0.0] * 10 \
        + [0.0] * 8 + [0.121, 0.486, 1.075, 0.379, 1.191, 0.659, 0.09] + [0.0] * 9
    for targets in ({0: 0.31}, {0: 0.30, 1: 0.45}):
        r = optimize(_prices(day + day), deficit, 0.24, start_hour=9, pv_surplus_kw=surplus, day_targets=targets)
        _replay(r, deficit, surplus, 0.24, floors={24 * b + 23: t for b, t in targets.items()})
        today = {d["hour"]: d for d in r["blocks"][0]["decisions"]}
        assert not today[22]["ac_on"] and today[13]["ac_on"] and not r["warnings"], (targets, r["warnings"])
        assert all(b["summary"]["target_reached"] is not False for b in r["blocks"]), targets


def check_first_hour_fraction():
    # A plan made at 22:30 has half of that hour left: 80 A moves 12.5 % of
    # SOC there, not 25 %, and costs half; the next hour is a whole one.
    r = optimize(_prices(PRICES), LOAD, 0.10, target_soc=0.9, start_hour=22, first_hour_fraction=0.5)
    first, second = r["decisions"]
    assert first["charge_amps"] == second["charge_amps"] == 80, r["decisions"]
    assert first["charge_wh"] == 2080 and second["charge_wh"] == 4160, r["decisions"]
    assert abs(first["soc_pct"] - 22.5) < 0.05 and abs(second["soc_pct"] - 47.5) < 0.05, r["decisions"]
    grid_kw = LOAD[22] + 80 * NOMINAL_VOLTAGE / 1000 / CHARGE_EFFICIENCY
    assert abs(first["grid_kwh"] - grid_kw / 2) < 1e-3, (first, grid_kw)
    assert abs(first["grid_cost_pln"] - PRICES[22] * grid_kw / 2) < 1e-3, first


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
    check_cheapest_hour_full_first()
    check_cheaper_hour_left_off()
    check_forced_hour_is_no_excuse()
    check_first_hour_fraction()
    check_min_cost_per_kwh_still_works()
    print("optimizer self-check OK")
