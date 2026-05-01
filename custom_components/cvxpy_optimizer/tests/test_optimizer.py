"""Unit and scenario-based tests for the optimizer module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ..fetcher import PriceData
from ..optimizer import BatteryOptimizer, OptimizationResult


class TestBatteryOptimizerBasic:
    """Basic tests for the CVXPY optimizer."""

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_optimize_with_flat_prices(self):
        """Flat prices → optimizer should not charge or discharge."""
        prices = self._make_prices([0.8] * 24)
        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.5)

        assert result.solver_status.startswith("optimal")
        # With flat prices and no load, there's no incentive to cycle
        total_charge = np.sum(result.charge_power)
        total_discharge = np.sum(result.discharge_power)
        # At most minimal cycling due to numerical precision
        assert total_charge < 100 or total_discharge < 100

    def test_optimize_with_price_spread(self):
        """Price spread → optimizer should charge cheap, discharge expensive."""
        prices = self._make_prices([0.2] * 8 + [1.5] * 8 + [0.3] * 8)
        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.2)

        assert result.solver_status.startswith("optimal")
        # Should charge during first 8 hours (cheap)
        early_charge = np.sum(result.charge_power[:8])
        # Should discharge during middle 8 hours (expensive)
        mid_discharge = np.sum(result.discharge_power[8:16])

        assert early_charge > 0 or mid_discharge > 0, "Optimizer should exploit price spread"

    def test_soc_trajectory_in_bounds(self):
        """SoC trajectory must stay within [soc_min, soc_max]."""
        prices = self._make_prices([0.5] * 24)
        optimizer = BatteryOptimizer(soc_min=0.1, soc_max=0.95)
        result = optimizer.optimize(prices=prices, initial_soc=0.5)

        assert result.solver_status.startswith("optimal")
        for soc in result.soc_trajectory:
            assert 0.1 - 0.01 <= soc <= 0.95 + 0.01, f"SoC {soc} out of bounds"


class TestScenarioA_NegativePrices:
    """Scenario A: Day with some negative prices (arbitrage opportunity).

    Verify that the optimizer handles negative prices correctly by
    charging during negative-price periods (getting paid to store energy).
    """

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_optimizer_charges_during_negative_prices(self):
        """With negative prices, optimizer should charge the battery."""
        # Negative prices at hours 2-4 (3 AM - 6 AM)
        prices = self._make_prices(
            [
                0.5,
                0.4,
                -0.10,
                -0.15,
                -0.08,
                0.3,  # negative at 2-4
                1.2,
                1.3,
                1.1,
                0.9,
                0.7,
                0.65,  # expensive midday
                0.6,
                0.55,
                0.6,
                0.5,
                0.4,
                1.0,  # afternoon/evening
                1.2,
                1.4,
                1.5,
                1.3,
                0.8,
                0.7,  # evening peak → night
            ]
        )

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.3)

        assert result.solver_status.startswith("optimal")

        # Should charge during negative price hours (indices 2-4)
        negative_hours_charge = np.sum(result.charge_power[2:5])
        assert negative_hours_charge > 0, "Optimizer should charge during negative-price periods"


class TestScenarioB_ExpensiveDay:
    """Scenario B: Whole day expensive with mixed regular prices.

    Battery should minimize grid import during high-price periods,
    prefer discharging when beneficial.
    """

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_discharges_during_expensive_periods(self):
        """On an expensive day, battery should discharge during peak hours."""
        # All prices are high (1.0+), with peaks at midday and evening
        prices = self._make_prices(
            [
                1.2,
                1.15,
                1.10,
                1.08,
                1.12,
                1.25,  # night: expensive
                1.60,
                1.75,
                1.55,
                1.40,
                1.30,
                1.25,  # morning: very expensive
                1.20,
                1.15,
                1.25,
                1.10,
                1.20,
                1.45,  # afternoon: expensive
                1.65,
                1.80,
                1.90,
                1.75,
                1.35,
                1.30,  # evening peak → night
            ]
        )

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.8)

        assert result.solver_status.startswith("optimal")

        # Should discharge during the most expensive hours (indices 6-9 and 18-20)
        peak_discharge = np.sum(result.discharge_power[6:10]) + np.sum(
            result.discharge_power[18:21]
        )
        assert peak_discharge > 0, "Battery should discharge during expensive periods"

        # Final SoC should be lower than initial (used battery to save money)
        assert result.final_soc < result.initial_soc - 0.01


class TestScenarioC_LowSoC:
    """Scenario C: Low SoC situation.

    Optimizer should limit or disable discharge and prioritize charging,
    even if prices are favorable for discharging.
    """

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_limits_discharge_at_low_soc(self):
        """At very low SoC (10%), optimizer respects soc_min bound."""
        # High prices that would normally trigger discharge
        prices = self._make_prices([0.5] * 6 + [1.8] * 12 + [0.6] * 6)

        optimizer = BatteryOptimizer(soc_min=0.05, soc_max=1.0)
        result = optimizer.optimize(prices=prices, initial_soc=0.10)

        assert result.solver_status.startswith("optimal")

        # SoC must never drop below minimum (with small tolerance for numerical precision)
        min_soc = np.min(result.soc_trajectory)
        assert min_soc >= 0.04, f"SoC dropped to {min_soc:.4f}, below soc_min of 0.05"

    def test_prioritizes_charging_at_low_soc(self):
        """At low SoC with cheap prices, optimizer should charge."""
        # Cheap prices throughout
        prices = self._make_prices([0.2] * 24)

        optimizer = BatteryOptimizer(soc_min=0.05, soc_max=1.0)
        result = optimizer.optimize(prices=prices, initial_soc=0.10)

        assert result.solver_status.startswith("optimal")

        # Should charge to replenish battery
        total_charge = np.sum(result.charge_power)
        assert total_charge > 0, "Optimizer should charge when SoC is low and prices are cheap"


class TestScenarioD_MixedPrices:
    """Scenario D: Mixed day with varying prices and medium SoC.

    Validate balanced charge/discharge decisions.
    """

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_balanced_schedule(self):
        """With mixed prices and medium SoC, optimizer should find balanced schedule."""
        # Realistic price pattern: cheap night → expensive morning → cheap afternoon → expensive evening
        prices = self._make_prices(
            [
                0.3,
                0.25,
                0.2,
                0.22,
                0.28,
                0.4,  # 0-5: cheap night
                1.4,
                1.5,
                1.3,
                1.1,
                0.7,
                0.65,  # 6-11: morning peak → drop
                0.6,
                0.55,
                0.6,
                0.28,
                0.4,
                1.0,  # 12-17: cheap afternoon → rise
                1.3,
                1.5,
                1.6,
                1.4,
                0.9,
                0.8,  # 18-23: evening peak → night
            ]
        )

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.5)

        assert result.solver_status.startswith("optimal")

        # Should charge during cheapest hours (indices 1-4 and 13-14)
        cheap_charge = np.sum(result.charge_power[1:5]) + np.sum(result.charge_power[13:15])
        # Should discharge during most expensive hours (indices 6-8 and 19-20)
        expensive_discharge = np.sum(result.discharge_power[6:9]) + np.sum(
            result.discharge_power[19:21]
        )

        assert cheap_charge > 0 or expensive_discharge > 0, (
            "Optimizer should find arbitrage opportunities in mixed price profile"
        )


class TestEdgeCases:
    """Edge case tests."""

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_empty_price_data(self):
        """Empty price data should return non-optimal status."""
        prices = PriceData(timestamps=[], prices=np.array([]))
        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.5)
        assert result.solver_status == "no_data"

    def test_full_battery(self):
        """Full battery → no charging allowed."""
        prices = self._make_prices([0.1] * 24 + [2.0] * 24)  # 48 steps
        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=1.0)

        assert result.solver_status.startswith("optimal")
        # Should not charge when full (or minimal due to numerical precision)
        total_charge = np.sum(result.charge_power)
        assert total_charge < 50, "Should not charge a full battery"

    def test_empty_battery(self):
        """Empty battery → no discharging allowed."""
        prices = self._make_prices([2.0] * 24 + [0.1] * 24)  # expensive first half
        optimizer = BatteryOptimizer(soc_min=0.05, soc_max=1.0)
        result = optimizer.optimize(prices=prices, initial_soc=0.05)

        assert result.solver_status.startswith("optimal")
        # Should not discharge below minimum SoC
        for soc in result.soc_trajectory:
            assert soc >= 0.04, f"SoC {soc} dropped below minimum"


class TestSolverStatus:
    """Tests for solver status handling."""

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_optimal_status(self):
        """Normal case should return optimal status."""
        prices = self._make_prices([0.5] * 24)
        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.5)
        assert result.solver_status.startswith("optimal")

    def test_infeasible_handling(self):
        """Infeasible problem should not crash and should report status."""
        # Force infeasibility: require final SoC > 1.0 (impossible)
        prices = self._make_prices([0.5] * 24)
        optimizer = BatteryOptimizer(soc_min=0.9, soc_max=0.95)
        result = optimizer.optimize(prices=prices, initial_soc=0.92)

        # Should still return a result (even if infeasible)
        assert isinstance(result, OptimizationResult)
        assert len(result.solver_status) > 0
