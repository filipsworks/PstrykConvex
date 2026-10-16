"""CVXPY optimization model for home battery scheduling."""

import sys

import cvxpy as cp
import numpy as np
from battery_model import (
    CAPACITY_AH,
    CHARGE_EFFICIENCY,
    CHARGE_STEPS_A,
    DISCHARGE_EFFICIENCY,
    DUMMY_LOAD_KW,
    GRID_LIMIT_KW,
    MAX_CHARGE_CURRENT_A,
    MAX_CHARGE_POWER_KW,
    MAX_SOC,
    MAX_TOTAL_CHARGE_CURRENT_A,
    MIN_SOC,
    NOMINAL_VOLTAGE,
    TOTAL_CAPACITY_WH,
)

# Objective mode constants
OBJECTIVE_MIN_COST = "min_cost"
OBJECTIVE_MIN_COST_PER_KWH = "min_cost_per_kwh"
VALID_OBJECTIVES = (OBJECTIVE_MIN_COST, OBJECTIVE_MIN_COST_PER_KWH)

# Tie-breakers, far below any real price difference: offgrid wins every hour
# it costs nothing extra, and PV surplus goes into the battery as soon as it
# appears, like the inverter does.
_EPS = 1e-4

# PLN per kWh of a soft limit missed — far above any real price, so the solver
# only misses a limit when no plan can keep it.
_PENALTY = 1000.0

# Per-solve cap for the MILP (seconds); see the solve call.
MIP_TIME_LIMIT_S = 3


def _summarize(decisions: list[dict], over_kwh: float, target_reached) -> dict:
    """Totals over a run of hourly decisions (whole horizon or one day)."""
    charge_wh = sum(d["charge_wh"] for d in decisions)
    discharge_wh = sum(d["discharge_wh"] for d in decisions)
    return {
        "total_charge_wh": round(charge_wh, 1),
        "total_discharge_wh": round(discharge_wh, 1),
        "total_pv_charge_wh": round(sum(d["pv_charge_wh"] for d in decisions), 1),
        "net_energy_wh": round(charge_wh - discharge_wh, 1),
        "cycled_pct": round(discharge_wh / TOTAL_CAPACITY_WH * 100, 2),
        "total_grid_kwh": round(sum(d["grid_kwh"] for d in decisions), 3),
        "total_cost_pln": round(sum(d["grid_cost_pln"] for d in decisions), 4),
        "total_pv_lost_kwh": round(sum(d["pv_lost_kwh"] for d in decisions), 3),
        "ac_hours": sum(1 for d in decisions if d["ac_on"]),
        "final_soc": decisions[-1]["soc_pct"] if decisions else None,
        "min_soc": min(d["soc_pct"] for d in decisions) if decisions else None,
        # None = no target for this span.
        "target_reached": target_reached,
        "grid_over_limit_kwh": round(over_kwh, 3),
    }


def _build_and_solve(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    target_soc: float = None,
    start_hour: int = 0,
    max_soc=MAX_SOC,
    min_soc: float = MIN_SOC,
    pv_surplus_kw: list[float] | None = None,
    day_targets: dict[int, float] | None = None,
) -> dict:
    """Core MILP: minimise grid cost for hours [start_hour, len(prices)).

    The horizon is one block of any length — the rest of today, tomorrow once
    its prices are out, and estimated hours after that — solved jointly, so
    today's plan already knows what tomorrow needs.  Index 0 is 00:00 of the
    first day.

    ``dummy_loads_kw`` is the house load behind the inverter left after PV
    (``max(0, consumption − PV)``) — despite the historical name it is NOT
    the negative-price dummy circuit.  ``pv_surplus_kw`` is the opposite side
    (``max(0, PV − consumption)``): it charges the battery for free; whatever
    does not fit is curtailed (``pv_lost_kwh``) — nothing is exported.

    Offgrid has priority and there is no plain bypass: every hour runs in one
    of two modes, modelled the way the inverter behaves:

      SBU/OSO  offgrid — battery covers the WHOLE house deficit (AC off)
      SUB/SNU  AC on — grid covers the deficit and charges at k × 10 A, k ≥ 1

    Grid import (house deficit + charging) shares the 25 A breaker with the
    dummy circuit, which runs whenever the price is negative.  Negative
    prices need no other special case: charging and grid supply earn money
    then, so the solver fills the most negative hours first on its own.

    ``max_soc`` is a fraction or a per-hour list (e.g. 1.0 on a monthly
    balance day); ``min_soc`` is the reserve the SOC never drops below.
    ``target_soc`` pins the SOC at the horizon end; ``day_targets`` maps a
    day index ``b`` to the SOC at its end (hour ``(b + 1) * 24``) when that
    boundary is inside the horizon.  Both are soft.
    """
    HOURS = len(prices)
    n = HOURS - start_hour
    capacity_kwh = TOTAL_CAPACITY_WH / 1000
    eta_ch = CHARGE_EFFICIENCY
    eta_dis = DISCHARGE_EFFICIENCY
    step_a = CHARGE_STEPS_A[1]
    step_kw = step_a * NOMINAL_VOLTAGE / 1000  # battery-side kW per charge step
    k_max = MAX_CHARGE_CURRENT_A // step_a
    max_batt_in_kw = MAX_TOTAL_CHARGE_CURRENT_A * NOMINAL_VOLTAGE / 1000  # PV + grid
    max_discharge_kw = NOMINAL_VOLTAGE * CAPACITY_AH / 1000 * 0.5

    price = np.array([p["price"] for p in prices[start_hour:HOURS]], dtype=float)
    deficit = np.array(dummy_loads_kw[start_hour:HOURS], dtype=float)
    surplus = (
        np.array(pv_surplus_kw[start_hour:HOURS], dtype=float)
        if pv_surplus_kw is not None
        else np.zeros(n)
    )
    grid_cap = GRID_LIMIT_KW - np.where(price < 0, DUMMY_LOAD_KW, 0.0)

    # SOC already outside [min, max] (PV tops the pack up to 100 %, the BMS may
    # read below MinSOC) must not make the problem infeasible.
    soc_lo = min(min_soc, initial_soc)
    soc_hi = np.maximum(np.broadcast_to(np.asarray(max_soc, dtype=float), (HOURS,))[start_hour:], initial_soc)

    # Soft SOC floors at hour boundaries: {hours from start: SOC fraction}.
    # Whole 10 A steps can't land exactly on MaxSOC without crossing it, so a
    # target at the ceiling means "full to within one charge step".
    targets: dict[int, float] = {}
    if target_soc is not None:
        targets[n] = target_soc
    for b, tgt in (day_targets or {}).items():
        t = (b + 1) * 24 - start_hour
        if 0 < t <= n:
            targets[t] = max(tgt, targets.get(t, 0.0))
    targets = {t: min(v, soc_hi[t - 1] - step_kw / capacity_kwh) for t, v in sorted(targets.items())}

    # --- Variables ---
    sbu = cp.Variable(n, boolean=True)   # 1 = offgrid, 0 = AC on and charging
    k = cp.Variable(n, integer=True)     # grid charge current in steps of step_a
    pv_in = cp.Variable(n, nonneg=True)  # kW of PV surplus taken by the battery
    soc = cp.Variable(n + 1)             # SOC fraction at each hour boundary

    grid_charge = k * (step_kw / eta_ch)                   # grid-side kW
    grid_load = cp.multiply(deficit, 1 - sbu)              # grid → house kW
    discharge = cp.multiply(deficit / eta_dis, sbu)        # battery-side kW

    # The breaker cap and the SOC targets are soft: a plan always exists, so
    # the solver never reports infeasible.  The house alone can exceed what
    # negative-price hours leave next to the dummy circuit, and late in the
    # day a target may be out of reach.
    over = cp.Variable(n, nonneg=True)   # kW of grid import over the breaker cap
    # SOC missing to each target (cvxpy has no 0-size variables; a spare one stays 0).
    short = cp.Variable(max(1, len(targets)), nonneg=True)

    constraints = [
        soc[0] == initial_soc,
        soc[1:] == soc[:-1]
        + (k * step_kw + eta_ch * pv_in - discharge) / capacity_kwh,
        soc[1:] >= soc_lo,
        soc[1:] <= soc_hi,
        # AC on only to charge: a grid hour always takes at least one step.
        k >= 1 - sbu,
        k <= k_max * (1 - sbu),
        pv_in <= surplus,
        k * step_kw + eta_ch * pv_in <= max_batt_in_kw,
        discharge <= max_discharge_kw,
        grid_charge <= MAX_CHARGE_POWER_KW,
        # ponytail: hourly averages — a short peak above the average can
        # still trip the breaker; lower GRID_LIMIT_KW if that happens.
        grid_load + grid_charge <= grid_cap + over,
    ]
    for i, (t, tgt) in enumerate(targets.items()):
        constraints.append(soc[t] + short[i] >= tgt)
        print(f"  → SOC ≥ {tgt * 100:.0f}% after {t} h", file=sys.stderr)

    # Earlier PV intake scores higher, so the battery takes PV as soon as it
    # comes. ponytail: the solver may still skip morning PV to leave room for
    # paid grid charging in later negative hours, which the inverter can't do;
    # needs a "battery full" binary per hour if that ever shows up in a plan.
    pv_weight = _EPS * np.arange(n, 0, -1) / n
    objective = cp.Minimize(
        price @ (grid_load + grid_charge) - _EPS * cp.sum(sbu) - pv_weight @ pv_in
        + _PENALTY * (cp.sum(over) + cp.sum(short) * capacity_kwh)
    )

    problem = cp.Problem(objective, constraints)
    # Real (distinct) hourly prices solve in well under a second.  Runs of
    # identical prices make many plans tie and HiGHS can grind on proving
    # which is best — past the limit, take the best plan found so far.
    problem.solve(
        solver=cp.HIGHS, verbose=False, time_limit=MIP_TIME_LIMIT_S, mip_rel_gap=1e-3
    )

    timed_out_with_plan = problem.status == "user_limit" and sbu.value is not None
    if problem.status not in ("optimal", "optimal_inaccurate") and not timed_out_with_plan:
        raise RuntimeError(f"Optimization failed: {problem.status}")

    # --- Build results ---
    decisions = []
    for t in range(n):
        h = start_hour + t
        is_sbu = round(float(sbu.value[t])) == 1
        steps = 0 if is_sbu else int(round(float(k.value[t])))
        pv_kw = max(0.0, float(pv_in.value[t]))

        charge_wh = round(steps * step_kw * 1000, 2)
        discharge_wh = round(deficit[t] / eta_dis * 1000, 2) if is_sbu else 0.0
        gl_kw = 0.0 if is_sbu else deficit[t]
        gc_kw = steps * step_kw / eta_ch
        grid_cost = price[t] * (gl_kw + gc_kw)
        total_active_kw = deficit[t] + gc_kw

        decisions.append(
            {
                "hour": h % 24,
                # offgrid = AC breaker off, battery + PV; charge = AC on.
                "mode": "offgrid" if is_sbu else "charge",
                "ac_on": not is_sbu,
                "output_mode": "SBU" if is_sbu else "SUB",
                "charger_mode": "OSO" if is_sbu else "SNU",
                "charge_wh": charge_wh,
                "discharge_wh": discharge_wh,
                "pv_charge_wh": round(eta_ch * pv_kw * 1000, 2),
                # PV the battery can't take; curtailed, never exported.
                "pv_lost_kwh": round(max(0.0, surplus[t] - pv_kw), 3),
                "charge_amps": steps * step_a,
                "soc_pct": round(float(soc.value[t + 1]) * 100, 1),
                "grid_kwh": round(gl_kw + gc_kw, 3),
                "grid_cost_pln": round(grid_cost, 4),
                "total_active_kw": round(total_active_kw, 3),
                "total_cost_pln": round(price[t] * total_active_kw, 4),
                "price_plkwh": float(price[t]),
                "is_estimated": bool(prices[h].get("is_estimated", False)),
            }
        )

    over_kwh = np.maximum(over.value, 0.0)
    reached = {t: float(short.value[i]) < 1e-3 for i, t in enumerate(targets)}

    # One block per calendar day (block 0 = the day of index 0).
    blocks = []
    for b in range(start_hour // 24, (HOURS - 1) // 24 + 1):
        ts = [t for t in range(n) if (start_hour + t) // 24 == b]
        if not ts:
            continue
        end_t = ts[-1] + 1
        blocks.append(
            {
                "decisions": decisions[ts[0]:end_t],
                "summary": _summarize(
                    decisions[ts[0]:end_t], float(over_kwh[ts].sum()), reached.get(end_t)
                ),
            }
        )

    summary = _summarize(
        decisions, float(over_kwh.sum()), all(reached.values()) if reached else None
    )

    warnings = []
    for t, ok in reached.items():
        if not ok:
            warnings.append(
                f"Target SOC {targets[t] * 100:.0f}% unreachable — "
                f"best possible is {float(soc.value[t]) * 100:.0f}%"
            )
    over_hours = [start_hour + t for t in range(n) if over_kwh[t] > 1e-3]
    if over_hours:
        warnings.append(
            f"House load exceeds the breaker cap in horizon hours {over_hours} "
            f"({summary['grid_over_limit_kwh']} kWh over)"
        )

    return {"decisions": decisions, "summary": summary, "warnings": warnings, "blocks": blocks}


def _find_min_cost_per_kwh_target_soc(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    start_hour: int = 0,
    step_pct: int = 5,
    max_soc: float = MAX_SOC,
    min_soc: float = MIN_SOC,
    pv_surplus_kw: list[float] | None = None,
) -> tuple[float | None, float]:
    """Sweep target SOC levels and return the one with minimum PLN/kWh.

    Implements the "red-line minimum" from the sensitivity chart: scans the
    entire feasible target-SOC range and returns the point where
    total_cost_pln / total_grid_kwh is lowest.

    Returns:
        (best_target_soc_fraction, best_cost_per_kwh)
        best_target_soc_fraction is None if no feasible point found.
    """
    best_target_soc: float | None = None
    best_cost_per_kwh = float("inf")

    soc_levels_pct = list(range(int(min_soc * 100), int(np.max(max_soc) * 100) + 1, step_pct))
    print(
        f"  [min_cost_per_kwh] Sweeping {len(soc_levels_pct)} SOC targets "
        f"({soc_levels_pct[0]}%–{soc_levels_pct[-1]}%, step {step_pct}%)…",
        file=sys.stderr,
    )

    for target_pct in soc_levels_pct:
        target_frac = target_pct / 100.0
        try:
            result = _build_and_solve(
                prices, dummy_loads_kw, initial_soc,
                target_soc=target_frac,
                start_hour=start_hour,
                max_soc=max_soc,
                min_soc=min_soc,
                pv_surplus_kw=pv_surplus_kw,
            )
            summary = result["summary"]
            if not summary["target_reached"]:
                break  # targets ascend, so every higher one is out of reach too
            grid_kwh = summary["total_grid_kwh"]
            cost_pln = summary["total_cost_pln"]
            if grid_kwh <= 0:
                continue
            cost_per_kwh = cost_pln / grid_kwh
            if cost_per_kwh < best_cost_per_kwh:
                best_cost_per_kwh = cost_per_kwh
                best_target_soc = target_frac
        except RuntimeError:
            pass  # Solver gave no plan at this target — skip silently

    if best_target_soc is None:
        print(
            "  [min_cost_per_kwh] Sweep found no feasible point — "
            "falling back to unconstrained",
            file=sys.stderr,
        )
    else:
        print(
            f"  [min_cost_per_kwh] Optimal target SOC: "
            f"{best_target_soc * 100:.0f}%  "
            f"({best_cost_per_kwh:.4f} PLN/kWh)",
            file=sys.stderr,
        )

    return best_target_soc, best_cost_per_kwh


def optimize(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    target_soc: float = None,
    start_hour: int = 0,
    objective: str = OBJECTIVE_MIN_COST,
    max_soc=MAX_SOC,
    min_soc: float = MIN_SOC,
    pv_surplus_kw: list[float] | None = None,
    day_targets: dict[int, float] | None = None,
) -> dict:
    """Run the CVXPY optimisation and return results.

    Two objective modes are supported:

    ``min_cost`` (default)
        Minimise total grid spend in PLN.  Classic mode: cheapest overall
        bill.  An optional *target_soc* can set a minimum end-of-day SOC.

    ``min_cost_per_kwh``
        Minimise average price paid per kWh consumed.  Internally runs a
        target-SOC sweep, picks the SOC level that yields the lowest
        PLN/kWh ratio, then solves the model once with that constraint.
        This corresponds to the red-line minimum on the sensitivity chart.
        Any caller-supplied *target_soc* is ignored in this mode.

    Negative-price hours need no special case: grid import earns money then,
    so the solver charges in the most negative hours on its own, within what
    the 25 A breaker leaves next to the dummy circuit.

    Args:
        prices: hourly price dicts with 'hour', 'price' keys (and optional
                'is_estimated'); 24 for one day, more for a joint horizon.
        dummy_loads_kw: house load behind the inverter left after PV, one
                        per price (kW, ≥ 0). Not the negative-price dummy circuit.
        initial_soc: SOC at start_hour (0–1).
        target_soc: Optional SOC floor at the horizon end (0–1). Ignored for
                    min_cost_per_kwh (the sweep determines it).
        start_hour: First index to optimise (past hours of day 0 skipped).
        objective: 'min_cost' or 'min_cost_per_kwh'.
        max_soc: Upper SOC bound (0–1), or one per price. Pass 1.0 for a
                 scheduled monthly balance / full-charge day. Defaults to :data:`MAX_SOC`.
        min_soc: Reserve the SOC stays above (0–1). Defaults to :data:`MIN_SOC`.
        pv_surplus_kw: PV surplus over the house load, one per price (kW, ≥ 0).
                       Charges the battery for free. None = no PV.
        day_targets: {day index: SOC floor at the end of that day}.

    Returns:
        dict with keys:
          - decisions: list of hourly dicts for indices [start_hour, len(prices))
          - blocks: the same decisions split per calendar day, each with a summary
          - summary: horizon totals
          - objective: which mode was used
          - chosen_target_soc_pct: (min_cost_per_kwh only) SOC% chosen by sweep
          - chosen_cost_per_kwh: (min_cost_per_kwh only) PLN/kWh at chosen SOC
    """
    if objective not in VALID_OBJECTIVES:
        raise ValueError(
            f"Unknown objective '{objective}'. Valid: {VALID_OBJECTIVES}"
        )

    if not (0.0 <= min_soc < np.min(max_soc) and np.max(max_soc) <= 1.0):
        raise ValueError(
            f"Invalid SOC bounds: min_soc={min_soc}, max_soc={max_soc} "
            "(require 0 ≤ min < max ≤ 1)."
        )

    chosen_target_soc = target_soc
    chosen_cost_per_kwh = None

    if objective == OBJECTIVE_MIN_COST_PER_KWH:
        chosen_target_soc, chosen_cost_per_kwh = _find_min_cost_per_kwh_target_soc(
            prices, dummy_loads_kw, initial_soc, start_hour=start_hour,
            max_soc=max_soc, min_soc=min_soc, pv_surplus_kw=pv_surplus_kw,
        )

    result = _build_and_solve(
        prices, dummy_loads_kw, initial_soc,
        target_soc=chosen_target_soc,
        start_hour=start_hour,
        max_soc=max_soc,
        min_soc=min_soc,
        pv_surplus_kw=pv_surplus_kw,
        day_targets=day_targets,
    )

    result["objective"] = objective
    result["max_soc_pct"] = round(float(np.max(max_soc)) * 100, 1)
    result["min_soc_pct"] = round(min_soc * 100, 1)
    if objective == OBJECTIVE_MIN_COST_PER_KWH:
        result["chosen_target_soc_pct"] = (
            round(chosen_target_soc * 100, 0) if chosen_target_soc is not None else None
        )
        result["chosen_cost_per_kwh"] = (
            round(chosen_cost_per_kwh, 5) if chosen_cost_per_kwh is not None else None
        )

    return result
