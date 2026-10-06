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
    MIN_SOC,
    NOMINAL_VOLTAGE,
    TOTAL_CAPACITY_WH,
)

# Objective mode constants
OBJECTIVE_MIN_COST = "min_cost"
OBJECTIVE_MIN_COST_PER_KWH = "min_cost_per_kwh"
VALID_OBJECTIVES = (OBJECTIVE_MIN_COST, OBJECTIVE_MIN_COST_PER_KWH)

# Tie-breakers, far below any real price difference: without a deficit to
# cover SBU and SUB/OSO are equivalent (prefer SUB/OSO), and PV surplus goes
# into the battery as soon as it appears, like the inverter does.
_EPS = 1e-4

# PLN per kWh of a soft limit missed — far above any real price, so the solver
# only misses a limit when no plan can keep it.
_PENALTY = 1000.0

# Per-solve cap for the MILP (seconds); see the solve call.
MIP_TIME_LIMIT_S = 3


def _build_and_solve(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    target_soc: float = None,
    start_hour: int = 0,
    max_soc: float = MAX_SOC,
    min_soc: float = MIN_SOC,
    pv_surplus_kw: list[float] | None = None,
) -> dict:
    """Core MILP: minimise grid cost for hours [start_hour, 24).

    ``dummy_loads_kw`` is the house load behind the inverter left after PV
    (``max(0, consumption − PV)``) — despite the historical name it is NOT
    the negative-price dummy circuit.  ``pv_surplus_kw`` is the opposite side
    (``max(0, PV − consumption)``): it charges the battery for free; whatever
    does not fit is lost, nothing is exported.

    Every hour runs in exactly one inverter mode, modelled the way the
    inverter behaves rather than as free power flows:

      SBU/OSO  battery covers the WHOLE house deficit
      SUB/OSO  grid covers the deficit, battery holds (PV may still charge it)
      SUB/SNU  grid covers the deficit and charges at k × 10 A

    Grid import (house deficit + charging) shares the 25 A breaker with the
    dummy circuit, which runs whenever the price is negative.  Negative
    prices need no other special case: charging and grid supply earn money
    then, so the solver fills the most negative hours first on its own.

    ``max_soc`` and ``min_soc`` are fractions (0–1).  They override the
    module-level :data:`MAX_SOC` / :data:`MIN_SOC` defaults so the caller can
    schedule e.g. monthly full-charge balance days (``max_soc=1.0``) without
    touching the rest of the code.
    """
    HOURS = 24
    n = HOURS - start_hour
    capacity_kwh = TOTAL_CAPACITY_WH / 1000
    eta_ch = CHARGE_EFFICIENCY
    eta_dis = DISCHARGE_EFFICIENCY
    step_a = CHARGE_STEPS_A[1]
    step_kw = step_a * NOMINAL_VOLTAGE / 1000  # battery-side kW per charge step
    k_max = MAX_CHARGE_CURRENT_A // step_a
    max_batt_in_kw = MAX_CHARGE_CURRENT_A * NOMINAL_VOLTAGE / 1000  # PV + grid
    max_discharge_kw = NOMINAL_VOLTAGE * CAPACITY_AH / 1000 * 0.5

    price = np.array([p["price"] for p in prices[start_hour:HOURS]], dtype=float)
    deficit = np.array(dummy_loads_kw[start_hour:HOURS], dtype=float)
    surplus = (
        np.array(pv_surplus_kw[start_hour:HOURS], dtype=float)
        if pv_surplus_kw is not None
        else np.zeros(n)
    )
    grid_cap = GRID_LIMIT_KW - np.where(price < 0, DUMMY_LOAD_KW, 0.0)

    # --- Variables ---
    sbu = cp.Variable(n, boolean=True)   # battery powers the house
    snu = cp.Variable(n, boolean=True)   # grid charges the battery
    k = cp.Variable(n, integer=True)     # grid charge current in steps of step_a
    pv_in = cp.Variable(n, nonneg=True)  # kW of PV surplus taken by the battery
    soc = cp.Variable(n + 1)             # SOC fraction at each hour boundary

    grid_charge = k * (step_kw / eta_ch)                   # grid-side kW
    grid_load = cp.multiply(deficit, 1 - sbu)              # grid → house kW
    discharge = cp.multiply(deficit / eta_dis, sbu)        # battery-side kW

    # SOC already outside [min, max] (PV tops the pack up to 100 %, the BMS may
    # read below MinSOC) must not make the problem infeasible.
    soc_lo = min(min_soc, initial_soc)
    soc_hi = max(max_soc, initial_soc)

    if target_soc is not None:
        # Whole 10 A steps can't land exactly on MaxSOC without crossing it,
        # so a target at the ceiling means "full to within one charge step".
        target_soc = min(target_soc, soc_hi - step_kw / capacity_kwh)

    # The breaker cap and the EOD target are soft: a plan always exists (hold
    # the battery, house on the grid), so the solver never reports infeasible.
    # The house alone can exceed what negative-price hours leave next to the
    # dummy circuit, and late in the day a target may be out of reach.
    over = cp.Variable(n, nonneg=True)   # kW of grid import over the breaker cap
    short = cp.Variable(nonneg=True)     # EOD SOC missing to the target

    constraints = [
        soc[0] == initial_soc,
        soc[1:] == soc[:-1]
        + (k * step_kw + eta_ch * pv_in - discharge) / capacity_kwh,
        soc[1:] >= soc_lo,
        soc[1:] <= soc_hi,
        sbu + snu <= 1,
        k >= snu,
        k <= k_max * snu,
        pv_in <= surplus,
        k * step_kw + eta_ch * pv_in <= max_batt_in_kw,
        discharge <= max_discharge_kw,
        grid_charge <= MAX_CHARGE_POWER_KW,
        # ponytail: hourly averages — a short peak above the average can
        # still trip the breaker; lower GRID_LIMIT_KW if that happens.
        grid_load + grid_charge <= grid_cap + over,
    ]
    if target_soc is not None:
        constraints.append(soc[n] + short >= target_soc)
        print(f"  → EOD target SOC ≥ {target_soc * 100:.0f}%", file=sys.stderr)

    # Earlier PV intake scores higher, so the battery takes PV as soon as it
    # comes. ponytail: the solver may still skip morning PV to leave room for
    # paid grid charging in later negative hours, which the inverter can't do;
    # needs a "battery full" binary per hour if that ever shows up in a plan.
    pv_weight = _EPS * np.arange(n, 0, -1) / n
    objective = cp.Minimize(
        price @ (grid_load + grid_charge) + _EPS * cp.sum(sbu) - pv_weight @ pv_in
        + _PENALTY * (cp.sum(over) + short * capacity_kwh)
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
    total_charge_wh = 0.0
    total_discharge_wh = 0.0
    total_pv_charge_wh = 0.0
    total_grid_kwh = 0.0
    total_cost_pln = 0.0

    for t in range(n):
        h = start_hour + t
        is_sbu = round(float(sbu.value[t])) == 1
        steps = int(round(float(k.value[t])))
        charge_amps = steps * step_a
        pv_kw = max(0.0, float(pv_in.value[t]))

        charge_wh = round(steps * step_kw * 1000, 2)
        discharge_wh = round(deficit[t] / eta_dis * 1000, 2) if is_sbu else 0.0
        pv_charge_wh = round(eta_ch * pv_kw * 1000, 2)
        gl_kw = 0.0 if is_sbu else deficit[t]
        gc_kw = steps * step_kw / eta_ch
        grid_cost = price[t] * (gl_kw + gc_kw)

        total_charge_wh += charge_wh
        total_discharge_wh += discharge_wh
        total_pv_charge_wh += pv_charge_wh
        total_grid_kwh += gl_kw + gc_kw
        total_cost_pln += grid_cost

        if is_sbu:
            output_mode, charger_mode = "SBU", "OSO"
        elif steps > 0:
            output_mode, charger_mode = "SUB", "SNU"
        else:
            output_mode, charger_mode = "SUB", "OSO"

        total_active_kw = deficit[t] + gc_kw

        decisions.append(
            {
                "hour": h,
                "output_mode": output_mode,
                "charger_mode": charger_mode,
                "charge_wh": charge_wh,
                "discharge_wh": discharge_wh,
                "pv_charge_wh": pv_charge_wh,
                "charge_amps": charge_amps,
                "soc_pct": round(float(soc.value[t + 1]) * 100, 1),
                "grid_cost_pln": round(grid_cost, 4),
                "total_active_kw": round(total_active_kw, 3),
                "total_cost_pln": round(price[t] * total_active_kw, 4),
                "price_plkwh": float(price[t]),
            }
        )

    summary = {
        "total_charge_wh": round(total_charge_wh, 1),
        "total_discharge_wh": round(total_discharge_wh, 1),
        "total_pv_charge_wh": round(total_pv_charge_wh, 1),
        "net_energy_wh": round(total_charge_wh - total_discharge_wh, 1),
        "cycled_pct": round((total_discharge_wh / TOTAL_CAPACITY_WH) * 100, 2),
        "total_grid_kwh": round(total_grid_kwh, 3),
        "total_cost_pln": round(total_cost_pln, 4),
        "final_soc": round(float(soc.value[n]) * 100, 1),
        # None = no target set.
        "target_reached": None if target_soc is None else float(short.value) < 1e-3,
        "grid_over_limit_kwh": round(float(np.sum(over.value)), 3),
    }

    warnings = []
    if summary["target_reached"] is False:
        warnings.append(
            f"Target SOC {target_soc * 100:.0f}% unreachable — "
            f"best possible is {summary['final_soc']:.0f}%"
        )
    over_hours = [start_hour + t for t in range(n) if over.value[t] > 1e-3]
    if over_hours:
        warnings.append(
            f"House load exceeds the breaker cap in hours {over_hours} "
            f"({summary['grid_over_limit_kwh']} kWh over)"
        )

    return {"decisions": decisions, "summary": summary, "warnings": warnings}


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

    soc_levels_pct = list(range(int(min_soc * 100), int(max_soc * 100) + 1, step_pct))
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
    max_soc: float = MAX_SOC,
    min_soc: float = MIN_SOC,
    pv_surplus_kw: list[float] | None = None,
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
        prices: 24 hourly price dicts with 'hour', 'price' keys.
        dummy_loads_kw: 24-hour house load behind the inverter left after PV
                        (kW, ≥ 0). Not the negative-price dummy circuit.
        initial_soc: SOC at start_hour (0–1).
        target_soc: Optional EOD SOC floor (0–1). Ignored for
                    min_cost_per_kwh (the sweep determines it).
        start_hour: First hour to optimise (default 0 = full day).
        objective: 'min_cost' or 'min_cost_per_kwh'.
        max_soc: Upper SOC bound (0–1). Pass 1.0 for a scheduled monthly
                 balance / full-charge day. Defaults to :data:`MAX_SOC`.
        min_soc: Lower SOC bound (0–1). Defaults to :data:`MIN_SOC`.
        pv_surplus_kw: 24-hour PV surplus over the house load (kW, ≥ 0).
                       Charges the battery for free. None = no PV.

    Returns:
        dict with keys:
          - decisions: list of hourly dicts for hours [start_hour, 24)
          - summary: daily totals
          - objective: which mode was used
          - chosen_target_soc_pct: (min_cost_per_kwh only) SOC% chosen by sweep
          - chosen_cost_per_kwh: (min_cost_per_kwh only) PLN/kWh at chosen SOC
    """
    if objective not in VALID_OBJECTIVES:
        raise ValueError(
            f"Unknown objective '{objective}'. Valid: {VALID_OBJECTIVES}"
        )

    if not (0.0 <= min_soc < max_soc <= 1.0):
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
    )

    result["objective"] = objective
    result["max_soc_pct"] = round(max_soc * 100, 1)
    result["min_soc_pct"] = round(min_soc * 100, 1)
    if objective == OBJECTIVE_MIN_COST_PER_KWH:
        result["chosen_target_soc_pct"] = (
            round(chosen_target_soc * 100, 0) if chosen_target_soc is not None else None
        )
        result["chosen_cost_per_kwh"] = (
            round(chosen_cost_per_kwh, 5) if chosen_cost_per_kwh is not None else None
        )

    return result
