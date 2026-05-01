"""Unit tests for the fetcher module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.core import State

from ..fetcher import BatteryState, Fetcher, PriceData


class TestPriceData:
    """Tests for the PriceData container."""

    def test_has_negative_prices_true(self):
        prices = PriceData(
            timestamps=[datetime.now(timezone.utc)] * 3,
            prices=[0.5, -0.1, 0.8],
        )
        assert prices.has_negative_prices == True

    def test_has_negative_prices_false(self):
        prices = PriceData(
            timestamps=[datetime.now(timezone.utc)] * 3,
            prices=[0.5, 0.6, 0.7],
        )
        assert prices.has_negative_prices == False

    def test_min_max_avg(self):
        prices = PriceData(
            timestamps=[datetime.now(timezone.utc)] * 4,
            prices=[1.0, 2.0, 0.5, 3.0],
        )
        assert prices.min_price == pytest.approx(0.5)
        assert prices.max_price == pytest.approx(3.0)
        assert prices.avg_price == pytest.approx(1.625)


class TestBatteryState:
    """Tests for the BatteryState container."""

    def test_is_available_with_voltage(self):
        state = BatteryState(voltage=26.4, current=5.0, soc=0.5)
        assert state.is_available is True

    def test_not_available_without_voltage(self):
        state = BatteryState(voltage=None, current=None, soc=None)
        assert state.is_available is False


class TestFetcherVoltageToSoC:
    """Tests for the voltage-to-SoC conversion."""

    @pytest.mark.parametrize(
        "voltage,soc_expected",
        [
            (24.0, 0.05),  # Minimum → 5% SOC
            (28.0, 1.0),  # Maximum → 100% SOC
            (26.4, pytest.approx(0.5)),  # Mid-range ≈ 50% SOC
        ],
    )
    def test_voltage_to_soc(self, voltage, soc_expected):
        result = Fetcher._voltage_to_soc(voltage)
        assert result == pytest.approx(soc_expected, abs=0.01)

    def test_voltage_below_min_clamps_to_5pct(self):
        result = Fetcher._voltage_to_soc(23.0)
        assert result == 0.05

    def test_voltage_above_max_clamps_to_100pct(self):
        result = Fetcher._voltage_to_soc(29.0)
        assert result == 1.0


class TestFetcherFetchBatteryState:
    """Tests for fetching battery state from HA."""

    def test_fetches_all_battery_sensors(self, mock_hass_with_data):
        fetcher = Fetcher(hass=mock_hass_with_data)
        state = asyncio_run(fetcher.fetch_battery_state())

        assert state.voltage == pytest.approx(26.4)
        assert state.current == pytest.approx(5.0)
        assert state.soc is not None
        assert 0.0 < state.soc <= 1.0

    def test_returns_none_for_missing_voltage_sensor(self, mock_hass):
        mock_hass.states.get.return_value = None
        fetcher = Fetcher(hass=mock_hass)
        state = asyncio_run(fetcher.fetch_battery_state())

        assert state.voltage is None
        assert state.current is None
        assert state.soc is None


class TestFetcherFetchPrices:
    """Tests for fetching price data from HA."""

    def test_fetches_today_prices(self, mock_hass):
        today_prices = [
            {
                "start": f"2026-04-28T{i:02d}:00:00+02:00",
                "end": f"2026-04-28T{i + 1:02d}:00:00+02:00",
                "price": 0.5 + i * 0.1,
            }
            for i in range(24)
        ]
        mock_hass.states.get.return_value = State(
            entity_id="sensor.pstryk_aio_obecna_cena_zakupu_pradu",
            state="0.85",
            attributes={"today_prices": today_prices},
        )

        fetcher = Fetcher(hass=mock_hass)
        prices = asyncio_run(fetcher.fetch_today_prices())

        assert prices is not None
        assert len(prices.prices) == 24
        assert prices.prices[0] == pytest.approx(0.5)

    def test_returns_none_for_missing_price_sensor(self, mock_hass):
        mock_hass.states.get.return_value = None
        fetcher = Fetcher(hass=mock_hass)
        prices = asyncio_run(fetcher.fetch_today_prices())
        assert prices is None

    def test_returns_none_for_empty_price_list(self, mock_hass):
        mock_hass.states.get.return_value = State(
            entity_id="sensor.pstryk_aio_obecna_cena_zakupu_pradu",
            state="0.85",
            attributes={},  # No today_prices attribute
        )
        fetcher = Fetcher(hass=mock_hass)
        prices = asyncio_run(fetcher.fetch_today_prices())
        assert prices is None


class TestFetcherFetchLoad:
    """Tests for fetching load data from HA."""

    def test_fetches_current_load(self, mock_hass):
        mock_hass.states.get.return_value = State(
            entity_id="sensor.gniazdo_output_active_power",
            state="150.5",
            attributes={},
        )
        fetcher = Fetcher(hass=mock_hass)
        load = asyncio_run(fetcher.fetch_current_load())
        assert load == pytest.approx(150.5)

    def test_returns_none_for_missing_load_sensor(self, mock_hass):
        mock_hass.states.get.return_value = None
        fetcher = Fetcher(hass=mock_hass)
        load = asyncio_run(fetcher.fetch_current_load())
        assert load is None


# ── Helper ────────────────────────────────────────────────────────────────


def asyncio_run(coro):
    """Run an async function synchronously in tests."""
    import asyncio

    _loop = asyncio.new_event_loop()
    try:
        return _loop.run_until_complete(coro)
    finally:
        _loop.close()
