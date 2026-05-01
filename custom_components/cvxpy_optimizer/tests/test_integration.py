"""E2E-style integration tests for the full optimization pipeline.

Tests the complete flow: fetcher → optimizer → decider → service output.
Includes randomized pricing tests with various characteristics.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest
from homeassistant.core import State

from ..decider import ModeDecider
from ..fetcher import BatteryState, Fetcher, PriceData
from ..optimizer import BatteryOptimizer


class TestFullPipeline:
    """Test the complete fetcher → optimizer → decider pipeline."""

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_full_pipeline_normal_day(self):
        """Normal day with realistic prices → valid mode decision."""
        prices = self._make_prices(
            [
                0.86,
                0.82,
                0.81,
                0.80,
                0.82,
                0.87,
                1.41,
                1.37,
                1.24,
                1.02,
                0.68,
                0.67,
                0.66,
                0.63,
                0.67,
                0.26,
                0.38,
                1.11,
                1.30,
                1.44,
                1.64,
                1.40,
                0.89,
                0.83,
            ]
        )

        battery = BatteryState(voltage=26.4, current=5.0, soc=0.45)

        optimizer = BatteryOptimizer()
        opt_result = optimizer.optimize(prices=prices, initial_soc=0.45)

        assert opt_result.solver_status.startswith("optimal")

        decider = ModeDecider()
        decision = decider.decide(
            prices=prices,
            battery_state=battery,
            opt_result=opt_result,
        )

        assert decision.discharge_mode in ("SUB", "SBU")
        assert decision.charger_mode in ("CSO", "SNU", "OSO")
        assert len(decision.reason) > 0


class TestMockedHAIntegration:
    """Test with fully mocked HA environment."""

    def test_complete_flow_with_mocked_hass(self, mock_hass_with_data):
        """Complete flow using mocked HA sensors."""
        fetcher = Fetcher(hass=mock_hass_with_data)

        # Mock price sensor
        today_prices = [
            {
                "start": f"2026-04-28T{i:02d}:00:00+02:00",
                "end": f"2026-04-28T{i + 1:02d}:00:00+02:00",
                "price": 0.5 + i * 0.05,
            }
            for i in range(24)
        ]

        def get_side_effect(eid):
            states_map = {
                "sensor.gniazdo_battery_voltage": State(
                    "sensor.gniazdo_battery_voltage", "26.4", {"unit_of_measurement": "V"}
                ),
                "sensor.gniazdo_battery_discharge_current": State(
                    "sensor.gniazdo_battery_discharge_current", "3.0", {"unit_of_measurement": "A"}
                ),
                "sensor.gniazdo_output_active_power": State(
                    "sensor.gniazdo_output_active_power", "100.0", {}
                ),
                "sensor.pstryk_aio_obecna_cena_zakupu_pradu": State(
                    "sensor.pstryk_aio_obecna_cena_zakupu_pradu",
                    "0.85",
                    {"today_prices": today_prices},
                ),
            }
            return states_map.get(eid)

        mock_hass_with_data.states.get.side_effect = get_side_effect

        async def run_test():
            import asyncio

            prices = await fetcher.fetch_today_prices()
            battery_state = await fetcher.fetch_battery_state()
            load = await fetcher.fetch_current_load()

            assert prices is not None
            assert len(prices.prices) == 24
            assert battery_state.is_available
            assert battery_state.soc is not None
            assert load is not None

            optimizer = BatteryOptimizer()
            opt_result = optimizer.optimize(
                prices=prices,
                initial_soc=battery_state.soc or 0.5,
                load_power=np.full(24, load),
            )

            decider = ModeDecider()
            decision = decider.decide(
                prices=prices, battery_state=battery_state, opt_result=opt_result
            )

            assert decision.discharge_mode in ("SUB", "SBU")
            assert decision.charger_mode in ("CSO", "SNU", "OSO")

        # Run async test
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run_test())
        finally:
            loop.close()


class TestRandomizedPricing:
    """Tests with randomized price vectors of different characteristics."""

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_volatile_prices(self):
        """Highly volatile prices → optimizer should find many arbitrage opportunities."""
        random.seed(42)
        prices = [random.uniform(-0.2, 2.0) for _ in range(24)]
        price_data = self._make_prices(prices)

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=price_data, initial_soc=0.5)

        assert result.solver_status.startswith("optimal")
        # Should have some charge/discharge activity
        total_activity = np.sum(result.charge_power) + np.sum(result.discharge_power)
        assert total_activity > 0

    def test_mostly_negative_prices(self):
        """Mostly negative prices → heavy charging, SNU mode."""
        random.seed(123)
        prices = [
            random.uniform(-0.5, -0.05) if i < 18 else random.uniform(0.5, 1.5) for i in range(24)
        ]
        price_data = self._make_prices(prices)

        battery = BatteryState(voltage=25.0, current=0.0, soc=0.3)
        optimizer = BatteryOptimizer()
        opt_result = optimizer.optimize(prices=price_data, initial_soc=0.3)

        assert opt_result.solver_status.startswith("optimal")

        decider = ModeDecider()
        decision = decider.decide(prices=price_data, battery_state=battery, opt_result=opt_result)

        assert decision.charger_mode == "SNU", (
            f"Expected SNU for mostly negative prices, got {decision.charger_mode}"
        )

    def test_flat_prices(self):
        """Flat prices → minimal cycling."""
        price_data = self._make_prices([0.8] * 24)

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=price_data, initial_soc=0.5)

        assert result.solver_status.startswith("optimal")
        # Minimal charge/discharge with flat prices (some numerical cycling is OK)
        total_charge = np.sum(result.charge_power)
        total_discharge = np.sum(result.discharge_power)
        # Should be close to zero (no incentive to cycle)
        assert max(total_charge, total_discharge) < 5000

    def test_spike_heavy_prices(self):
        """Price spikes → optimizer should discharge during peaks."""
        prices = [0.3] * 24
        # Add a spike at hours 7-9 (morning peak)
        for i in range(7, 10):
            prices[i] = 2.5

        price_data = self._make_prices(prices)

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=price_data, initial_soc=0.8)

        assert result.solver_status.startswith("optimal")

        # Should discharge during spike hours
        spike_discharge = np.sum(result.discharge_power[7:10])
        assert spike_discharge > 0, "Should discharge during price spikes"


class TestSoCEstimationAccuracy:
    """Tests for SoC estimation accuracy from voltage."""

    @pytest.mark.parametrize(
        "voltage,expected_soc_range",
        [
            (24.0, (0.04, 0.06)),  # Minimum → ~5%
            (24.8, (0.10, 0.15)),  # Low
            (25.6, (0.18, 0.22)),  # 20% mark
            (26.4, (0.48, 0.52)),  # Mid-range ≈ 50%
            (27.2, (0.78, 0.82)),  # 80% mark
            (27.6, (0.90, 0.95)),  # High
            (28.0, (0.99, 1.01)),  # Maximum → ~100%
        ],
    )
    def test_voltage_to_soc_accuracy(self, voltage, expected_soc_range):
        soc = Fetcher._voltage_to_soc(voltage)
        assert expected_soc_range[0] <= soc <= expected_soc_range[1], (
            f"SoC {soc} for voltage {voltage}V not in range {expected_soc_range}"
        )


class TestCostOptimization:
    """Verify that optimization actually reduces costs vs. naive strategy."""

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_optimized_cost_less_than_naive(self):
        """Optimized schedule should cost less than buying everything from grid."""
        # Variable prices with clear arbitrage opportunity
        prices = self._make_prices([0.2] * 8 + [1.5] * 8 + [0.3] * 8)

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.3)

        assert result.solver_status.startswith("optimal")

        # Naive cost: buy all load from grid at market prices (no battery)
        naive_cost = float(np.sum(prices.prices * np.ones(24)))  # Assume 1kW load each hour

        # Optimized cost should be lower
        assert result.total_cost < naive_cost, (
            f"Optimized cost {result.total_cost:.2f} should be less than naive {naive_cost:.2f}"
        )


class TestEdgeCasesIntegration:
    """Integration-level edge case tests."""

    def _make_prices(self, prices_list):
        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def test_missing_battery_sensor(self):
        """Missing battery sensor → optimizer should still run with default SoC."""
        prices = self._make_prices([0.5] * 24)
        battery = BatteryState(voltage=None, current=None, soc=None)

        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.5)

        assert result.solver_status.startswith("optimal")

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)
        # Should default to SUB discharge and CSO charger when SoC unknown
        assert decision.discharge_mode == "SUB"

    def test_zero_horizon(self):
        """Zero-length price data → graceful handling."""
        prices = PriceData(timestamps=[], prices=np.array([]))
        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.5)
        assert result.solver_status == "no_data"

    def test_single_time_step(self):
        """Single time step → minimal optimization."""
        prices = self._make_prices([0.8])
        optimizer = BatteryOptimizer()
        result = optimizer.optimize(prices=prices, initial_soc=0.5)
        assert len(result.charge_power) == 1
        assert len(result.discharge_power) == 1
