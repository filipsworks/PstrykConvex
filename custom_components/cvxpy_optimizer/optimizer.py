"""CVXPY-based optimization engine for battery charge/discharge scheduling.

Solves a linear programming problem to minimize total electricity cost over
a 24-hour horizon while respecting:
- Battery capacity and SoC limits
- Charge/discharge power limits (max current)
- Energy balance constraints
- Round-trip efficiency losses
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import cvxpy as cp
import numpy as np

from .const import (
    BATTERY_ENERGY_WH,
    CHARGE_EFFICIENCY,
    DISCHARGE_EFFICIENCY,
    HORIZON_HOURS,
    MAX_CHARGE_POWER,
    MAX_DISCHARGE_POWER,
    SOC_MAX,
    SOC_MIN,
    TIME_STEP_MINUTES,
)
from .fetcher import PriceData

_LOGGER = logging.getLogger(__name__)


@dataclass
class OptimizationResult:
    """Results from the battery optimization LP."""

    # Decision variables per time step
    charge_power: np.ndarray = field(
        default_factory=lambda: np.array([])
    )  # Watts (0 = not charging)
    discharge_power: np.ndarray = field(
        default_factory=lambda: np.array([])
    )  # Watts (0 = not discharging)

    # Derived values
    soc_trajectory: np.ndarray = field(
        default_factory=lambda: np.array([])
    )  # SoC fraction per time step
    grid_power: np.ndarray = field(
        default_factory=lambda: np.array([])
    )  # Net power from grid (positive = import, negative = export)

    # Objective value
    total_cost: float = 0.0

    # Solver status
    solver_status: str = ""

    # Initial and final SoC
    initial_soc: float = 0.0
    final_soc: float = 0.0


class BatteryOptimizer:
    """CVXPY-based LP optimizer for battery scheduling."""

    def __init__(
        self,
        horizon_hours: int = HORIZON_HOURS,
        time_step_minutes: int = TIME_STEP_MINUTES,
        max_charge_power: float = MAX_CHARGE_POWER,
        max_discharge_power: float = MAX_DISCHARGE_POWER,
        battery_energy_wh: float = BATTERY_ENERGY_WH,
        charge_efficiency: float = CHARGE_EFFICIENCY,
        discharge_efficiency: float = DISCHARGE_EFFICIENCY,
        soc_min: float = SOC_MIN,
        soc_max: float = SOC_MAX,
    ):
        """Initialize the optimizer with battery parameters.

        Args:
            horizon_hours: Number of hours in the optimization horizon.
            time_step_minutes: Time step size in minutes.
            max_charge_power: Maximum charging power in Watts.
            max_discharge_power: Maximum discharging power in Watts.
            battery_energy_wh: Total battery energy capacity in Wh.
            charge_efficiency: Charging efficiency factor (0–1).
            discharge_efficiency: Discharging efficiency factor (0–1).
            soc_min: Minimum State of Charge (fraction 0–1).
            soc_max: Maximum State of Charge (fraction 0–1).
        """
        self.horizon_hours = horizon_hours
        self.n_steps = horizon_hours * (60 // time_step_minutes)
        self.max_charge_power = max_charge_power
        self.max_discharge_power = max_discharge_power
        self.battery_energy_wh = battery_energy_wh
        self.charge_efficiency = charge_efficiency
        self.discharge_efficiency = discharge_efficiency
        self.soc_min = soc_min
        self.soc_max = soc_max

    def optimize(
        self,
        prices: PriceData,
        initial_soc: float,
        load_power: Optional[np.ndarray] = None,
    ) -> OptimizationResult:
        """Run the battery optimization LP.

        Minimizes total electricity cost over the horizon by deciding
        when to charge from grid and when to discharge to serve load.

        Args:
            prices: PriceData with timestamps and prices per time step.
            initial_soc: Initial State of Charge (0.0–1.0).
            load_power: Optional array of load power in Watts per time step.
                        If None, assumes zero load (battery only optimizes for price arbitrage).

        Returns:
            OptimizationResult with optimal charge/discharge schedule.
        """
        n = len(prices.prices)
        if n == 0:
            _LOGGER.warning("Empty price data — returning empty result")
            return OptimizationResult(solver_status="no_data")

        # Ensure arrays are the right length
        prices_arr = prices.prices[:n]

        if load_power is not None:
            load_arr = np.asarray(load_power, dtype=np.float64)[:n]
        else:
            load_arr = np.zeros(n)

        # ── Decision Variables ───────────────────────────────────────────
        # Charge power (W) — how much power goes INTO the battery from grid
        charge_p = cp.Variable(n, name="charge_power")

        # Discharge power (W) — how much power comes OUT of the battery
        discharge_p = cp.Variable(n, name="discharge_power")

        # SoC at each time step (fraction 0–1)
        soc = cp.Variable(n + 1, name="soc")

        # Grid power: positive = importing from grid, negative = exporting
        # grid_p = load - discharge + charge / efficiency_loss_adjustment
        # Simplified: grid_p = load - discharge_eff * discharge + charge / charge_eff
        grid_p = cp.Variable(n, name="grid_power")

        # ── Constraints ──────────────────────────────────────────────────
        constraints = []

        # Power limits
        constraints += [
            charge_p >= 0,
            charge_p <= self.max_charge_power,
            discharge_p >= 0,
            discharge_p <= self.max_discharge_power,
        ]

        # SoC bounds
        constraints += [
            soc[0] == initial_soc,
            soc[n] >= self.soc_min,  # End with at least minimum SoC
        ]

        # SoC dynamics: soc[t+1] = soc[t] + (charge_in * eff_ch - discharge_out / eff_dis) / capacity
        dt_hours = TIME_STEP_MINUTES / 60.0
        energy_per_step = self.battery_energy_wh  # Total Wh capacity
        soc_dynamics = []

        for t in range(n):
            charged_energy = charge_p[t] * self.charge_efficiency * dt_hours
            discharged_energy = discharge_p[t] * dt_hours / self.discharge_efficiency
            soc_dynamics.append(
                soc[t + 1] == soc[t] + (charged_energy - discharged_energy) / energy_per_step
            )
        constraints += soc_dynamics

        # SoC bounds at every step
        for t in range(n + 1):
            constraints.append(soc[t] >= self.soc_min)
            constraints.append(soc[t] <= self.soc_max)

        # Power balance: grid_power = load - discharge_eff * discharge + charge / charge_eff
        # This means: what we draw from the grid covers load minus battery discharge,
        # plus what we need to charge the battery (accounting for charging losses)
        for t in range(n):
            constraints.append(
                grid_p[t]
                == load_arr[t]
                - self.discharge_efficiency * discharge_p[t]
                + charge_p[t] / self.charge_efficiency
            )

        # ── Objective: Minimize total cost ───────────────────────────────
        # Cost = sum(price[t] * max(grid_p[t], 0)) — we only pay for import, not export credit
        # For simplicity with LP, assume prices are always positive (negative handled in decider)
        total_cost = cp.sum(cp.multiply(prices_arr, grid_p))

        problem = cp.Problem(cp.Minimize(total_cost), constraints)

        # ── Solve ────────────────────────────────────────────────────────
        try:
            problem.solve(
                solver=cp.CLARABEL,
                verbose=False,
            )
        except cp.SolverError as exc:
            _LOGGER.error("CVXPY solver error: %s", exc)
            return OptimizationResult(solver_status=f"solver_error: {exc}")

        result = OptimizationResult(
            charge_power=charge_p.value if charge_p.value is not None else np.zeros(n),
            discharge_power=discharge_p.value if discharge_p.value is not None else np.zeros(n),
            soc_trajectory=soc.value if soc.value is not None else np.full(n + 1, initial_soc),
            grid_power=grid_p.value if grid_p.value is not None else np.zeros(n),
            total_cost=float(problem.value) if problem.value is not None else 0.0,
            solver_status=str(problem.status),
            initial_soc=initial_soc,
            final_soc=float(soc.value[-1]) if soc.value is not None else initial_soc,
        )

        _LOGGER.info(
            "Optimization complete: status=%s, cost=%.2f PLN, initial_SOC=%.1f%%, final_SoC=%.1f%%",
            result.solver_status,
            result.total_cost,
            result.initial_soc * 100,
            result.final_soc * 100,
        )

        # Check for infeasibility
        if problem.status in ("infeasible", "unbounded"):
            _LOGGER.warning(
                "Optimization %s — initial_SoC=%.1f%%, prices range=[%.3f, %.3f]",
                problem.status,
                initial_soc * 100,
                prices.min_price,
                prices.max_price,
            )

        return result


def run_optimization(
    prices: PriceData,
    initial_soc: float,
    load_power: Optional[np.ndarray] = None,
) -> OptimizationResult:
    """Convenience function to run optimization with default parameters.

    Args:
        prices: Price data for the optimization horizon.
        initial_soc: Initial State of Charge (0.0–1.0).
        load_power: Optional load profile in Watts per time step.

    Returns:
        OptimizationResult with optimal schedule.
    """
    optimizer = BatteryOptimizer()
    return optimizer.optimize(prices=prices, initial_soc=initial_soc, load_power=load_power)
