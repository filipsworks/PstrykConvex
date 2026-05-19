"""CVXPY optimization model for home battery scheduling."""

import sys

import cvxpy as cp
import numpy as np
from battery_model import (
    CAPACITY_AH,
    CHARGE_EFFICIENCY,
    CHARGE_STEPS_A,
    DISCHARGE_EFFICIENCY,
    MAX_SOC,
    MIN_SOC,
    NOMINAL_VOLTAGE,
    TOTAL_CAPACITY_WH,
    max_charge_power_kw,
)

# Objective mode constants
OBJECTIVE_MIN_COST = "min_cost"
OBJECTIVE_MIN_COST_PER_KWH = "min_cost_per_kwh"
VALID_OBJECTIVES = (OBJECTIVE_MIN_COST, OBJECTIVE_MIN_COST_PER_KWH)


def _build_and_solve(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    target_soc: float = None,
    start_hour: int = 0,
    max_soc: float = MAX_SOC,
    min_soc: float = MIN_SOC,
) -> dict:
    """Core LP: minimize total grid cost subject to battery dynamics.

    This is the inner LP used by both objective modes.  It always minimises
    total PLN spend; the *target_soc* constraint is the only knob that
    differs between the two public objective modes.

    Negative-price hours are hard-constrained: discharge is forbidden and
    charging is forced to maximum rate (the separate dummy-load circuit that
    earns arbitrage revenue is not modelled in dummy_loads_kw).

    ``max_soc`` and ``min_soc`` are fractions (0–1).  They override the
    module-level :data:`MAX_SOC` / :data:`MIN_SOC` defaults so the caller can
    schedule e.g. monthly full-charge balance days (``max_soc=1.0``) without
    touching the rest of the code.
    """
    HOURS = 24
    capacity_wh = TOTAL_CAPACITY_WH
    eta_ch = CHARGE_EFFICIENCY
    eta_inv = DISCHARGE_EFFICIENCY
    max_p_charge_kw = min(max_charge_power_kw(), 2.0)
    max_discharge_kw = NOMINAL_VOLTAGE * CAPACITY_AH / 1000 * 0.5

    price_array = np.array([p["price"] for p in prices])

    # Hours where price < 0: always SUB/SNU at max charge current
    negative_price_hours = {h for h in range(HOURS) if price_array[h] < 0}

    # --- Variables ---
    charge = cp.Variable(HOURS, nonneg=True)     # kW grid → battery
    discharge = cp.Variable(HOURS, nonneg=True)  # kW battery → loads
    grid_load = cp.Variable(HOURS, nonneg=True)  # kW grid → dummy loads
    soc = cp.Variable(HOURS + 1)                 # SOC fraction at each boundary

    # --- Objective: minimise total grid cost for future hours ---
    objective = cp.Minimize(
        cp.sum(
            price_array[start_hour:] @ (grid_load[start_hour:] + charge[start_hour:])
        )
    )

    # --- Constraints ---
    constraints = []
    constraints.append(soc[start_hour] == initial_soc)

    for h in range(HOURS):
        dummy = dummy_loads_kw[h]

        if h < start_hour:
            # Past hours: nothing to optimise
            constraints.append(charge[h] == 0)
            constraints.append(discharge[h] == 0)
            constraints.append(grid_load[h] == dummy)
            constraints.append(soc[h + 1] == soc[h])
            continue

        if h in negative_price_hours:
            # Negative price: charge at maximum, no discharge, grid feeds load
            constraints.append(discharge[h] == 0)
            constraints.append(charge[h] == max_p_charge_kw)
            constraints.append(grid_load[h] == dummy)
        else:
            # Normal hour: load balance + power limits
            constraints.append(grid_load[h] + eta_inv * discharge[h] == dummy)
            constraints.append(charge[h] <= max_p_charge_kw)
            constraints.append(discharge[h] <= max_discharge_kw)
            constraints.append(charge[h] + discharge[h] >= 0.01)

        # SOC dynamics (all future hours)
        energy_in_wh = charge[h] * eta_ch * 1000
        energy_out_wh = discharge[h] / eta_inv * 1000
        constraints.append(
            soc[h + 1] == soc[h] + (energy_in_wh - energy_out_wh) / capacity_wh
        )
        constraints.append(soc[h + 1] >= min_soc)
        constraints.append(soc[h + 1] <= max_soc)

    if target_soc is not None:
        constraints.append(soc[HOURS] >= target_soc)
        print(f"  → EOD target SOC ≥ {target_soc * 100:.0f}%", file=sys.stderr)

    problem = cp.Problem(objective, constraints)
    problem.solve(solver=cp.HIGHS, verbose=False)

    if problem.status not in ("optimal", "optimal_inaccurate"):
        raise RuntimeError(f"Optimization failed: {problem.status}")

    # --- Snap charge power to discrete 10 A steps ---
    def snap_to_charge_step(power_kw: float) -> tuple[float, int]:
        if power_kw < 0.13:
            return 0.0, 0
        current_a = power_kw * 1000 / NOMINAL_VOLTAGE
        best_step = min(CHARGE_STEPS_A, key=lambda s: abs(s - current_a))
        return best_step * NOMINAL_VOLTAGE / 1000, best_step

    # --- Build results ---
    decisions = []
    total_charge_wh = 0.0
    total_discharge_wh = 0.0
    total_grid_kwh = 0.0
    total_cost_pln = 0.0
    soc_val = initial_soc

    for h in range(start_hour, HOURS):
        ch_val = float(charge[h].value) if charge[h].value is not None else 0.0
        dis_val = float(discharge[h].value) if discharge[h].value is not None else 0.0
        gl_val = float(grid_load[h].value) if grid_load[h].value is not None else 0.0
        soc_val = float(soc[h + 1].value) if soc[h + 1].value is not None else initial_soc

        ch_kw_snapped, charge_amps = snap_to_charge_step(ch_val)
        charge_wh = round(ch_kw_snapped * 1000, 2)
        discharge_wh = round(dis_val / DISCHARGE_EFFICIENCY * 1000, 2)

        if charge_wh < 0.5:
            charge_wh = 0.0
            charge_amps = 0
        if discharge_wh < 0.5:
            discharge_wh = 0.0

        total_charge_wh += charge_wh
        total_discharge_wh += discharge_wh
        total_grid_kwh += gl_val + ch_kw_snapped
        total_cost_pln += price_array[h] * (gl_val + ch_kw_snapped)

        # Mode assignment: negative-price hours are always SUB/SNU
        if h in negative_price_hours:
            output_mode = "SUB"
            charger_mode = "SNU"
        elif ch_val > 0.01:
            output_mode = "SUB"
            charger_mode = "SNU"
        else:
            output_mode = "SBU"
            charger_mode = "OSO"

        total_active_kw = dummy_loads_kw[h] + ch_kw_snapped
        grid_cost = price_array[h] * (gl_val + ch_kw_snapped)

        decisions.append(
            {
                "hour": h,
                "output_mode": output_mode,
                "charger_mode": charger_mode,
                "charge_wh": charge_wh,
                "discharge_wh": discharge_wh,
                "charge_amps": charge_amps,
                "soc_pct": round(soc_val * 100, 1),
                "grid_cost_pln": round(grid_cost, 4),
                "total_active_kw": round(total_active_kw, 3),
                "total_cost_pln": round(price_array[h] * total_active_kw, 4),
                "price_plkwh": price_array[h],
            }
        )

    summary = {
        "total_charge_wh": round(total_charge_wh, 1),
        "total_discharge_wh": round(total_discharge_wh, 1),
        "net_energy_wh": round(total_charge_wh - total_discharge_wh, 1),
        "cycled_pct": round((total_discharge_wh / capacity_wh) * 100, 2),
        "total_grid_kwh": round(total_grid_kwh, 3),
        "total_cost_pln": round(total_cost_pln, 4),
        "final_soc": round(soc_val * 100, 1),
    }

    return {"decisions": decisions, "summary": summary}


def _find_min_cost_per_kwh_target_soc(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    start_hour: int = 0,
    step_pct: int = 5,
    max_soc: float = MAX_SOC,
    min_soc: float = MIN_SOC,
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
            )
            summary = result["summary"]
            grid_kwh = summary["total_grid_kwh"]
            cost_pln = summary["total_cost_pln"]
            if grid_kwh <= 0:
                continue
            cost_per_kwh = cost_pln / grid_kwh
            if cost_per_kwh < best_cost_per_kwh:
                best_cost_per_kwh = cost_per_kwh
                best_target_soc = target_frac
        except RuntimeError:
            pass  # Infeasible at this target — skip silently

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
) -> dict:
    """Run the CVXPY optimisation and return results.

    Two objective modes are supported:

    ``min_cost`` (default)
        Minimise total grid spend in PLN.  Classic mode: cheapest overall
        bill.  An optional *target_soc* can set a minimum end-of-day SOC.

    ``min_cost_per_kwh``
        Minimise average price paid per kWh consumed.  Internally runs a
        target-SOC sweep, picks the SOC level that yields the lowest
        PLN/kWh ratio, then solves the LP once with that constraint.
        This corresponds to the red-line minimum on the sensitivity chart.
        Any caller-supplied *target_soc* is ignored in this mode.

    Negative-price hours are always handled as SUB/SNU at maximum charge
    current regardless of the objective mode.

    Args:
        prices: 24 hourly price dicts with 'hour', 'price' keys.
        dummy_loads_kw: 24-hour dummy load profile in kW.
        initial_soc: SOC at start_hour (0–1).
        target_soc: Optional EOD SOC floor (0–1). Ignored for
                    min_cost_per_kwh (the sweep determines it).
        start_hour: First hour to optimise (default 0 = full day).
        objective: 'min_cost' or 'min_cost_per_kwh'.
        max_soc: Upper SOC bound (0–1). Pass 1.0 for a scheduled monthly
                 balance / full-charge day. Defaults to :data:`MAX_SOC`.
        min_soc: Lower SOC bound (0–1). Defaults to :data:`MIN_SOC`.

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
            max_soc=max_soc, min_soc=min_soc,
        )

    result = _build_and_solve(
        prices, dummy_loads_kw, initial_soc,
        target_soc=chosen_target_soc,
        start_hour=start_hour,
        max_soc=max_soc,
        min_soc=min_soc,
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
