"""Unit tests for the decider module."""

from __future__ import annotations

import numpy as np
import pytest

from ..decider import ModeDecider, make_decision
from ..fetcher import BatteryState, PriceData
from ..optimizer import OptimizationResult


class TestModeDeciderNegativePrices:
    """Test that negative prices force SNU charger mode."""

    def _make_prices(self, prices_list):
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def _make_result(self, charge_power=None, discharge_power=None):
        n = 24
        cp_val = np.zeros(n) if charge_power is None else charge_power
        dp_val = np.zeros(n) if discharge_power is None else discharge_power
        return OptimizationResult(
            charge_power=cp_val,
            discharge_power=dp_val,
            soc_trajectory=np.linspace(0.5, 0.5, n + 1),
            grid_power=np.zeros(n),
            total_cost=0.0,
            solver_status="optimal",
            initial_soc=0.5,
            final_soc=0.5,
        )

    def test_negative_price_forces_snu(self):
        """Any negative price → charger mode must be SNU."""
        prices = self._make_prices([0.5] * 2 + [-0.1] + [0.5] * 21)
        battery = BatteryState(voltage=26.4, current=0.0, soc=0.5)
        result = self._make_result()

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)

        assert decision.charger_mode == "SNU", (
            f"Expected SNU for negative prices, got {decision.charger_mode}"
        )
        assert "Negative price" in decision.reason


class TestModeDeciderDischargeModes:
    """Test discharge mode selection logic."""

    def _make_prices(self, prices_list):
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def _make_result(self, charge_power=None, discharge_power=None):
        n = 24
        cp_val = np.zeros(n) if charge_power is None else charge_power
        dp_val = np.zeros(n) if discharge_power is None else discharge_power
        return OptimizationResult(
            charge_power=cp_val,
            discharge_power=dp_val,
            soc_trajectory=np.linspace(0.5, 0.3, n + 1),
            grid_power=np.zeros(n),
            total_cost=0.0,
            solver_status="optimal",
            initial_soc=0.5,
            final_soc=0.3,
        )

    def test_sbu_when_significant_discharge(self):
        """High discharge → SBU mode."""
        prices = self._make_prices([1.5] * 24)
        battery = BatteryState(voltage=27.0, current=0.0, soc=0.6)
        # Significant discharge planned (~800W average)
        result = self._make_result(
            discharge_power=np.full(24, 800.0),
        )

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)

        assert decision.discharge_mode == "SBU", (
            f"Expected SBU for significant discharge, got {decision.discharge_mode}"
        )

    def test_sub_when_low_soc(self):
        """Low SoC (< 20%) → SUB mode regardless of optimization."""
        prices = self._make_prices([1.5] * 24)
        battery = BatteryState(voltage=25.0, current=0.0, soc=0.15)
        result = self._make_result(discharge_power=np.full(24, 800.0))

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)

        assert decision.discharge_mode == "SUB", (
            f"Expected SUB for low SoC, got {decision.discharge_mode}"
        )

    def test_sub_when_unknown_soc(self):
        """Unknown SoC → default to SUB."""
        prices = self._make_prices([1.5] * 24)
        battery = BatteryState(voltage=None, current=None, soc=None)
        result = self._make_result()

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)

        assert decision.discharge_mode == "SUB"


class TestModeDeciderChargerModes:
    """Test charger mode selection logic."""

    def _make_prices(self, prices_list):
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def _make_result(self, charge_power=None):
        n = 24
        return OptimizationResult(
            charge_power=charge_power or np.zeros(n),
            discharge_power=np.zeros(n),
            soc_trajectory=np.linspace(0.5, 0.5, n + 1),
            grid_power=np.zeros(n),
            total_cost=0.0,
            solver_status="optimal",
            initial_soc=0.5,
            final_soc=0.5,
        )

    def test_osu_when_battery_full(self):
        """SoC > 90% → OSU (solar only)."""
        prices = self._make_prices([0.2] * 24)
        battery = BatteryState(voltage=27.8, current=0.0, soc=0.95)
        result = self._make_result()

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)

        assert decision.charger_mode == "OSO", (
            f"Expected OSU for full battery, got {decision.charger_mode}"
        )

    def test_cso_when_cheap_hours_exist(self):
        """4+ cheap hours → CSO."""
        prices = self._make_prices([0.2] * 6 + [1.5] * 18)  # 6 cheap hours
        battery = BatteryState(voltage=26.0, current=0.0, soc=0.4)
        result = self._make_result()

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)

        assert decision.charger_mode == "CSO", (
            f"Expected CSO for cheap hours, got {decision.charger_mode}"
        )

    def test_osu_when_no_cheap_hours(self):
        """No cheap hours → OSU."""
        prices = self._make_prices([1.5] * 24)  # All expensive
        battery = BatteryState(voltage=26.0, current=0.0, soc=0.4)
        result = self._make_result()

        decider = ModeDecider()
        decision = decider.decide(prices=prices, battery_state=battery, opt_result=result)

        assert decision.charger_mode == "OSO", (
            f"Expected OSU for no cheap hours, got {decision.charger_mode}"
        )


class TestMakeDecision:
    """Test the convenience function."""

    def _make_prices(self, prices_list):
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 4, 29, tzinfo=timezone.utc)
        timestamps = [base + timedelta(hours=i) for i in range(len(prices_list))]
        return PriceData(timestamps=timestamps, prices=np.array(prices_list))

    def _make_result(self):
        n = 24
        return OptimizationResult(
            charge_power=np.zeros(n),
            discharge_power=np.zeros(n),
            soc_trajectory=np.full(n + 1, 0.5),
            grid_power=np.zeros(n),
            total_cost=0.0,
            solver_status="optimal",
            initial_soc=0.5,
            final_soc=0.5,
        )

    def test_returns_mode_decision(self):
        prices = self._make_prices([0.5] * 24)
        battery = BatteryState(voltage=26.4, current=0.0, soc=0.5)
        result = self._make_result()

        decision = make_decision(prices=prices, battery_state=battery, opt_result=result)

        assert decision.discharge_mode in ("SUB", "SBU")
        assert decision.charger_mode in ("CSO", "SNU", "OSO")
        assert len(decision.reason) > 0


class TestScheduleSummary:
    """Test schedule summary generation."""

    def _make_result(self, charge_power, discharge_power):
        n = 24
        return OptimizationResult(
            charge_power=charge_power,
            discharge_power=discharge_power,
            soc_trajectory=np.linspace(0.5, 0.3, n + 1),
            grid_power=np.zeros(n),
            total_cost=12.5,
            solver_status="optimal",
            initial_soc=0.5,
            final_soc=0.3,
        )

    def test_summary_contains_key_metrics(self):
        decider = ModeDecider()
        result = self._make_result(
            charge_power=np.full(24, 500.0),
            discharge_power=np.full(24, 800.0),
        )
        summary = decider._build_schedule_summary(result)

        assert "total_steps" in summary
        assert "total_charge_wh" in summary
        assert "total_discharge_wh" in summary
        assert "estimated_cost_pln" in summary
        assert "initial_soc_pct" in summary
        assert "final_soc_pct" in summary

    def test_empty_result_summary(self):
        decider = ModeDecider()
        result = OptimizationResult(solver_status="no_data")
        summary = decider._build_schedule_summary(result)
        assert summary == {}
