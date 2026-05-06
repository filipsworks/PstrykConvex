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


def optimize(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    target_soc: float = None,
    start_hour: int = 0,
) -> dict:
    """Run the CVXPY optimization and return results.

    Pure LP formulation (no binary variables). The cost-minimization objective
    naturally prevents simultaneous charge+discharge since it wastes money
    (paying grid while draining battery with round-trip losses).

    Args:
        prices: 24 hourly price dicts with 'hour', 'price' keys.
        dummy_loads_kw: 24-hour dummy load profile in kW.
        initial_soc: SOC at start_hour (0–1). This should be the live battery SOC.
        target_soc: Optional target end-of-day SOC (0–1). If None, no constraint.
        start_hour: The first hour to optimize (default 0 = full day).
                    Hours before this are considered past and will not be optimized.

    Returns:
        dict with keys:
          - decisions: list of dicts for hours [start_hour, 24)
          - summary: daily totals
    """
    HOURS = 24
    capacity_wh = TOTAL_CAPACITY_WH
    eta_ch = CHARGE_EFFICIENCY
    eta_inv = DISCHARGE_EFFICIENCY
    max_p_charge_kw = min(max_charge_power_kw(), 2.0)  # cap at ~1.8 kW practical

    # Max discharge rate: C/2 → ~2.08 kW conservative
    max_discharge_kw = NOMINAL_VOLTAGE * CAPACITY_AH / 1000 * 0.5

    # --- Variables (all continuous, nonnegative) ---
    charge = cp.Variable(HOURS, nonneg=True)  # kW from grid to battery
    discharge = cp.Variable(HOURS, nonneg=True)  # kW from battery to loads
    grid_load = cp.Variable(HOURS, nonneg=True)  # kW from grid to dummy loads

    soc = cp.Variable(HOURS + 1)  # SOC fraction at each hour boundary

    # --- Objective: minimize total grid cost (only for future hours) ---
    price_array = np.array([p["price"] for p in prices])
    objective = cp.Minimize(
        cp.sum(
            price_array[start_hour:] @ (grid_load[start_hour:] + charge[start_hour:])
        )
    )

    # --- Constraints ---
    constraints = []

    # Initial SOC at start_hour
    constraints.append(soc[start_hour] == initial_soc)

    for h in range(HOURS):
        dummy = dummy_loads_kw[h]

        if h < start_hour:
            # Past hours: no charging or discharging allowed (already happened)
            constraints.append(charge[h] == 0)
            constraints.append(discharge[h] == 0)
            constraints.append(grid_load[h] == dummy)
            # SOC must be continuous through past hours (no actual optimization)
            energy_in_wh = 0  # no charge in the past
            energy_out_wh = 0  # no discharge tracked for past
            constraints.append(
                soc[h + 1] == soc[h] + (energy_in_wh - energy_out_wh) / capacity_wh
            )
            continue

        # Load satisfaction: grid supply + battery discharge (inverted) = dummy load demand
        constraints.append(grid_load[h] + eta_inv * discharge[h] == dummy)

        # Charge power limit
        constraints.append(charge[h] <= max_p_charge_kw)

        # Discharge power limit
        constraints.append(discharge[h] <= max_discharge_kw)

        # SOC dynamics: energy in/out converted from kW to Wh (×1000 for 1h window)
        energy_in_wh = charge[h] * eta_ch * 1000
        energy_out_wh = discharge[h] / eta_inv * 1000
        constraints.append(
            soc[h + 1] == soc[h] + (energy_in_wh - energy_out_wh) / capacity_wh
        )

        # SOC bounds
        constraints.append(soc[h + 1] >= MIN_SOC)
        constraints.append(soc[h + 1] <= MAX_SOC)

    # Target end-of-day SOC constraint (if specified)
    if target_soc is not None:
        constraints.append(soc[HOURS] >= target_soc)
        print(
            f"  → EOD target SOC ≥ {target_soc * 100:.0f}%",
            file=sys.stderr,
        )

    problem = cp.Problem(objective, constraints)
    # HIGHS is a robust LP/MIP solver — much better than OSQP for this problem
    problem.solve(solver=cp.HIGHS, verbose=False)

    if problem.status not in ("optimal", "optimal_inaccurate"):
        raise RuntimeError(f"Optimization failed: {problem.status}")

    # --- Snap charge power to discrete 10A steps (260W at nominal voltage) ---
    # Since CVXPY/HIGHS doesn't support integer variables, we post-process the LP solution.
    # Each hour's charge power is rounded to the nearest allowed step: 0, 10, 20, ..., 70A.
    def snap_to_charge_step(power_kw: float) -> tuple[float, int]:
        """Snap continuous power (kW) to nearest discrete charging step.

        Returns (snapped_power_kw, charge_amps).
        """
        if power_kw < 0.13:  # below 5A threshold → treat as zero
            return 0.0, 0
        # Convert kW → A at nominal voltage
        current_a = power_kw * 1000 / NOMINAL_VOLTAGE
        # Find nearest step
        best_step = min(CHARGE_STEPS_A, key=lambda s: abs(s - current_a))
        return best_step * NOMINAL_VOLTAGE / 1000, best_step

    # --- Build results (only for optimized hours) ---
    decisions = []
    total_charge_wh = 0.0
    total_discharge_wh = 0.0
    total_grid_kwh = 0.0
    total_cost_pln = 0.0

    for h in range(start_hour, HOURS):
        ch_val = float(charge[h].value) if charge[h].value is not None else 0.0
        dis_val = float(discharge[h].value) if discharge[h].value is not None else 0.0
        gl_val = float(grid_load[h].value) if grid_load[h].value is not None else 0.0

        soc_val = (
            float(soc[h + 1].value) if soc[h + 1].value is not None else initial_soc
        )

        # Snap charge power to discrete 10A steps and compute Wh from snapped value
        ch_kw_snapped, charge_amps = snap_to_charge_step(ch_val)
        charge_wh = round(ch_kw_snapped * 1000, 2)  # kW × 1h → Wh

        # Discharge: battery→inverter→loads; report battery-side energy (÷η_inv)
        discharge_wh = round(dis_val / DISCHARGE_EFFICIENCY * 1000, 2)

        # Treat tiny values as zero (floating point noise)
        if charge_wh < 0.5:
            charge_wh = 0.0
            charge_amps = 0
        if discharge_wh < 0.5:
            discharge_wh = 0.0

        total_charge_wh += charge_wh
        total_discharge_wh += discharge_wh
        # Grid energy: kW × 1h = kWh (use snapped value for accuracy)
        total_grid_kwh += gl_val + ch_kw_snapped
        # Cost: PLN/kWh × kW × 1h = PLN (use snapped value for accuracy)
        total_cost_pln += price_array[h] * (gl_val + ch_kw_snapped)

        # Determine mode pair from solution values:
        is_charging = charge_amps > 0
        is_discharging = dis_val > 0.01 and gl_val < dummy_loads_kw[h] * 0.99

        if is_charging:
            output_mode = "SUB"
            charger_mode = "SNU"
        elif is_discharging:
            if gl_val > dummy_loads_kw[h] * 0.99:
                output_mode = "SUB"
                charger_mode = "OSO"
            else:
                output_mode = "SBU"
                charger_mode = "OSO"
        else:
            output_mode = "SUB"
            charger_mode = "OSO"

        # Total active power cost: price × (dummy loads + charge from grid)
        total_active_kw = dummy_loads_kw[h] + ch_kw_snapped
        total_cost_pln_hour = price_array[h] * total_active_kw

        grid_cost = price_array[h] * (gl_val + ch_kw_snapped)  # actual grid spend

        decisions.append(
            {
                "hour": h,
                "output_mode": output_mode,
                "charger_mode": charger_mode,
                "charge_wh": charge_wh,
                "discharge_wh": discharge_wh,
                "charge_amps": charge_amps,  # discrete charging step (0 or 10-70)
                "soc_pct": round(soc_val * 100, 1),
                "grid_cost_pln": round(grid_cost, 4),
                "total_active_kw": round(total_active_kw, 3),
                "total_cost_pln": round(total_cost_pln_hour, 4),
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
