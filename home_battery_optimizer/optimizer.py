"""CVXPY optimization model for home battery scheduling."""

import sys

import cvxpy as cp
import numpy as np
from battery_model import (
    CHARGE_EFFICIENCY,
    DISCHARGE_EFFICIENCY,
    MAX_SOC,
    MIN_SOC,
    TOTAL_CAPACITY_WH,
    max_charge_power_kw,
)


def optimize(
    prices: list[dict],
    dummy_loads_kw: list[float],
    initial_soc: float,
    target_soc: float = None,
) -> dict:
    """Run the CVXPY optimization and return results.

    Pure LP formulation (no binary variables). The cost-minimization objective
    naturally prevents simultaneous charge+discharge since it wastes money
    (paying grid while draining battery with round-trip losses).

    Args:
        prices: 24 hourly price dicts with 'hour', 'price' keys.
        dummy_loads_kw: 24-hour dummy load profile in kW.
        initial_soc: SOC at hour 0 (0–1).
        target_soc: Optional target end-of-day SOC (0–1). If None, no constraint.

    Returns:
        dict with keys:
          - decisions: list of 24 dicts, one per hour
          - summary: daily totals
    """
    HOURS = 24
    capacity_wh = TOTAL_CAPACITY_WH
    eta_ch = CHARGE_EFFICIENCY
    eta_inv = DISCHARGE_EFFICIENCY
    max_p_charge_kw = min(max_charge_power_kw(), 2.0)  # cap at ~1.8 kW practical

    # Max discharge rate: C/2 → ~2.08 kW conservative
    max_discharge_kw = NOMINAL_V * CAPACITY_AH / 1000 * 0.5

    # --- Variables (all continuous, nonnegative) ---
    charge = cp.Variable(HOURS, nonneg=True)  # kW from grid to battery
    discharge = cp.Variable(HOURS, nonneg=True)  # kW from battery to loads
    grid_load = cp.Variable(HOURS, nonneg=True)  # kW from grid to dummy loads

    soc = cp.Variable(HOURS + 1)  # SOC fraction at each hour boundary

    # --- Objective: minimize total grid cost ---
    price_array = np.array([p["price"] for p in prices])
    objective = cp.Minimize(cp.sum(price_array @ (grid_load + charge)))

    # --- Constraints ---
    constraints = []

    # Initial SOC
    constraints.append(soc[0] == initial_soc)

    for h in range(HOURS):
        dummy = dummy_loads_kw[h]

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

    # --- Build results ---
    decisions = []
    total_charge_wh = 0.0
    total_discharge_wh = 0.0
    total_grid_kwh = 0.0
    total_cost_pln = 0.0

    for h in range(HOURS):
        ch_val = float(charge[h].value) if charge[h].value is not None else 0.0
        dis_val = float(discharge[h].value) if discharge[h].value is not None else 0.0
        gl_val = float(grid_load[h].value) if grid_load[h].value is not None else 0.0

        soc_val = (
            float(soc[h + 1].value) if soc[h + 1].value is not None else initial_soc
        )

        # Variables are in kW, 1-hour window → Wh for charge/discharge
        # Charge: grid→battery (already at battery side after efficiency in SOC)
        charge_wh = round(ch_val * 1000, 2)
        # Discharge: battery→inverter→loads; report battery-side energy (÷η_inv)
        discharge_wh = round(dis_val / DISCHARGE_EFFICIENCY * 1000, 2)

        # Treat tiny values as zero (floating point noise)
        if charge_wh < 0.5:
            charge_wh = 0.0
        if discharge_wh < 0.5:
            discharge_wh = 0.0

        total_charge_wh += charge_wh
        total_discharge_wh += discharge_wh
        # Grid energy: kW × 1h = kWh
        total_grid_kwh += gl_val + ch_val
        # Cost: PLN/kWh × kW × 1h = PLN
        total_cost_pln += price_array[h] * (gl_val + ch_val)

        # Determine mode pair from solution values:
        is_charging = ch_val > 0.01
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
        # This shows what the hour costs regardless of battery mode,
        # so you can compare SUB vs SBU decisions at a glance.
        total_active_kw = dummy_loads_kw[h] + ch_val
        total_cost_pln_hour = price_array[h] * total_active_kw

        grid_cost = price_array[h] * (gl_val + ch_val)  # actual grid spend

        decisions.append(
            {
                "hour": h,
                "output_mode": output_mode,
                "charger_mode": charger_mode,
                "charge_wh": charge_wh,
                "discharge_wh": discharge_wh,
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


# Module-level constants needed in optimize()
NOMINAL_V = 26.0
CAPACITY_AH = 320
