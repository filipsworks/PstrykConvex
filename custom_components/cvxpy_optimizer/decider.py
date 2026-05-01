"""Decision logic for translating optimization results into HA-compatible modes.

Converts the CVXPY optimizer's numerical charge/discharge schedule into
discrete operating mode strings that Home Assistant automations can consume:

Discharge Modes:
  SUB – Solar First (Utility + Solar priority, battery as backup)
  SBU – Solar + Battery First (Battery and solar prioritized over grid)

Charger Modes:
  CSO – Charge Solar + Utility
  SNU – Solar + Negative Utility (forced arbitrage when price < 0)
  OSO – Solar Only

Rule: If any price in the optimization horizon is negative, charger mode must switch to SNU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .const import (
    CHARGER_CSO,
    CHARGER_OSU,
    CHARGER_SNU,
    DISCHARGE_SBU,
    DISCHARGE_SUB,
)
from .fetcher import BatteryState, PriceData
from .optimizer import OptimizationResult

_LOGGER = logging.getLogger(__name__)


@dataclass
class ModeDecision:
    """Final mode decision for Home Assistant automations."""

    discharge_mode: str  # SUB or SBU
    charger_mode: str  # CSO, SNU, or OSU
    reason: str  # Human-readable explanation of the decision
    schedule_summary: dict = field(default_factory=dict)


class ModeDecider:
    """Translates optimization results into HA-compatible mode strings."""

    def __init__(self):
        pass

    def decide(
        self,
        prices: PriceData,
        battery_state: BatteryState,
        opt_result: OptimizationResult,
    ) -> ModeDecision:
        """Determine the optimal operating modes based on optimization results.

        Args:
            prices: Price data for the optimization horizon.
            battery_state: Current battery state (voltage, current, SoC).
            opt_result: Results from the CVXPY optimizer.

        Returns:
            ModeDecision with discharge_mode, charger_mode, and reasoning.
        """
        # ── Rule 1: Negative prices → SNU (arbitrage) ────────────────────
        if prices.has_negative_prices:
            return ModeDecision(
                discharge_mode=self._decide_discharge(prices, battery_state, opt_result),
                charger_mode=CHARGER_SNU,
                reason=(
                    f"Negative price detected (min={prices.min_price:.3f} PLN/kWh). "
                    f"Switching to SNU for arbitrage opportunity."
                ),
                schedule_summary=self._build_schedule_summary(opt_result),
            )

        # ── Rule 2: High prices → discharge battery (SBU) ────────────────
        discharge_mode = self._decide_discharge(prices, battery_state, opt_result)

        # ── Rule 3: Charger mode based on price profile ──────────────────
        charger_mode = self._decide_charger(prices, battery_state, opt_result)

        reason = self._build_reason(prices, battery_state, discharge_mode, charger_mode)

        return ModeDecision(
            discharge_mode=discharge_mode,
            charger_mode=charger_mode,
            reason=reason,
            schedule_summary=self._build_schedule_summary(opt_result),
        )

    def _decide_discharge(
        self,
        prices: PriceData,
        battery_state: BatteryState,
        opt_result: OptimizationResult,
    ) -> str:
        """Decide between SUB and SBU discharge modes.

        SBU (Solar + Battery First) is chosen when:
          - Battery has sufficient SoC (> 30%)
          - Prices are above average (discharge to avoid expensive grid import)
          - Optimization shows significant discharge potential

        SUB (Solar First) is the default — battery acts as backup only.
        """
        if battery_state.soc is None:
            _LOGGER.warning("SoC unknown, defaulting to SUB")
            return DISCHARGE_SUB

        # Low SoC → don't discharge aggressively
        if battery_state.soc < 0.20:
            return DISCHARGE_SUB

        # Check optimization result for discharge activity
        avg_discharge = np.mean(opt_result.discharge_power)
        max_possible_discharge = 1680.0  # ~8S LiFePO4 at 70A * 24V

        if avg_discharge > max_possible_discharge * 0.3:
            # Significant discharge planned → use SBU to prioritize battery
            return DISCHARGE_SBU

        # Check price spread — high variance means arbitrage opportunity
        price_std = np.std(prices.prices)
        price_range = prices.max_price - prices.min_price

        if price_range > 0.5 and battery_state.soc > 0.3:
            return DISCHARGE_SBU

        # Default: SUB (solar first, battery as backup)
        return DISCHARGE_SUB

    def _decide_charger(
        self,
        prices: PriceData,
        battery_state: BatteryState,
        opt_result: OptimizationResult,
    ) -> str:
        """Decide between CSO and OSU charger modes.

        CSO (Charge Solar + Utility) is chosen when:
          - Battery needs charging and there are cheap/low-price periods
          - Price arbitrage opportunity exists (charge during cheap hours)

        OSU (Solar Only) is chosen when:
          - Battery is near full or prices are uniformly high
          - No economic benefit to grid charging
        """
        if battery_state.soc is None:
            return CHARGER_CSO  # Default to allowing grid charge when unknown

        # Near-full battery → solar only
        if battery_state.soc > 0.90:
            return CHARGER_OSU

        # Check if optimization suggests charging from grid
        avg_charge = np.mean(opt_result.charge_power)
        max_possible_charge = 1680.0

        if avg_charge > max_possible_charge * 0.1:
            # Optimization wants to charge from grid → CSO
            return CHARGER_CSO

        # Check for cheap price periods (below average)
        below_avg_mask = prices.prices < prices.avg_price
        cheap_hours = np.sum(below_avg_mask)

        if cheap_hours >= 4:  # At least 4 hours of cheap prices
            return CHARGER_CSO

        # Default: OSU (solar only, don't charge from grid unless very cheap)
        return CHARGER_OSU

    def _build_reason(
        self,
        prices: PriceData,
        battery_state: BatteryState,
        discharge_mode: str,
        charger_mode: str,
    ) -> str:
        """Build a human-readable explanation of the mode decision."""
        parts = []

        soc_str = (
            f"SoC={battery_state.soc * 100:.0f}%"
            if battery_state.soc is not None
            else "SoC=unknown"
        )
        parts.append(soc_str)
        parts.append(f"price_range=[{prices.min_price:.3f}, {prices.max_price:.3f}]")

        if discharge_mode == DISCHARGE_SBU:
            parts.append("discharging to avoid high grid prices")
        else:
            parts.append("conserving battery (SUB mode)")

        if charger_mode == CHARGER_CSO:
            parts.append("allowing grid charging during cheap periods")
        elif charger_mode == CHARGER_OSU:
            parts.append("solar-only charging (prices too high for grid charge)")

        return "; ".join(parts)

    def _build_schedule_summary(self, opt_result: OptimizationResult) -> dict:
        """Build a summary of the optimization schedule."""
        n = len(opt_result.charge_power)
        if n == 0:
            return {}

        return {
            "total_steps": int(n),
            "total_charge_wh": float(np.sum(opt_result.charge_power) * (60 / 60)),
            "total_discharge_wh": float(np.sum(opt_result.discharge_power) * (60 / 60)),
            "estimated_cost_pln": round(opt_result.total_cost, 2),
            "initial_soc_pct": round(opt_result.initial_soc * 100, 1),
            "final_soc_pct": round(opt_result.final_soc * 100, 1),
        }


def make_decision(
    prices: PriceData,
    battery_state: BatteryState,
    opt_result: OptimizationResult,
) -> ModeDecision:
    """Convenience function to make a mode decision with default decider.

    Args:
        prices: Price data for the optimization horizon.
        battery_state: Current battery state.
        opt_result: Results from the CVXPY optimizer.

    Returns:
        ModeDecision with recommended modes.
    """
    decider = ModeDecider()
    return decider.decide(prices=prices, battery_state=battery_state, opt_result=opt_result)
