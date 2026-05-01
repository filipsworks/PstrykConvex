"""Shared fixtures for cvxpy_optimizer tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from homeassistant.core import HomeAssistant, State


@pytest.fixture
def mock_hass() -> MagicMock:
    """Create a mocked Home Assistant instance."""
    hass = MagicMock(spec=HomeAssistant)
    hass.states = MagicMock()
    hass.data = {}
    return hass


@pytest.fixture
def sample_today_prices() -> dict[str, Any]:
    """Generate realistic today price data (24 hourly slots)."""
    prices = [
        0.86,
        0.82,
        0.81,
        0.80,
        0.82,
        0.87,  # 00-05: low night prices
        1.41,
        1.37,
        1.24,
        1.02,
        0.68,
        0.67,  # 06-11: morning peak → midday drop
        0.66,
        0.63,
        0.67,
        0.26,
        0.38,
        1.11,  # 12-17: cheap afternoon → evening rise
        1.30,
        1.44,
        1.64,
        1.40,
        0.89,
        0.83,  # 18-23: expensive evening → night
    ]
    base = datetime(2026, 4, 28, tzinfo=timezone(timedelta(hours=2)))
    today_prices = []
    for i, price in enumerate(prices):
        today_prices.append(
            {
                "start": (base + timedelta(hours=i)).isoformat(),
                "end": (base + timedelta(hours=i + 1)).isoformat(),
                "price": price,
                "is_cheap": price < 0.7,
                "is_expensive": price > 1.2,
            }
        )
    return {
        "data_timestamp": "2026-04-28T20:55:57+02:00",
        "today_prices": today_prices,
        "average_price_today": 0.94,
    }


@pytest.fixture
def sample_tomorrow_prices() -> dict[str, Any]:
    """Generate realistic tomorrow price data (24 hourly slots)."""
    prices = [
        0.81,
        0.81,
        0.80,
        0.80,
        0.82,
        0.86,
        1.33,
        1.32,
        1.24,
        0.98,
        0.68,
        0.66,
        0.64,
        0.66,
        0.67,
        0.26,
        0.40,
        1.14,
        1.28,
        1.41,
        1.57,
        1.39,
        0.89,
        0.84,
    ]
    base = datetime(2026, 4, 29, tzinfo=timezone(timedelta(hours=2)))
    tomorrow_prices = []
    for i, price in enumerate(prices):
        tomorrow_prices.append(
            {
                "start": (base + timedelta(hours=i)).isoformat(),
                "end": (base + timedelta(hours=i + 1)).isoformat(),
                "price": price,
                "is_cheap": price < 0.7,
                "is_expensive": price > 1.2,
            }
        )
    return {
        "data_timestamp": "2026-04-28T20:55:57+02:00",
        "tomorrow_prices": tomorrow_prices,
        "average_price_tomorrow": 0.93,
    }


@pytest.fixture
def sample_negative_prices() -> dict[str, Any]:
    """Generate price data with negative prices (arbitrage scenario)."""
    prices = [
        0.50,
        0.40,
        -0.05,
        -0.10,
        -0.08,
        0.30,
        1.20,
        1.30,
        1.10,
        0.90,
        0.70,
        0.65,
        0.60,
        0.55,
        0.60,
        0.25,
        0.35,
        1.00,
        1.20,
        1.40,
        1.50,
        1.30,
        0.80,
        0.70,
    ]
    base = datetime(2026, 4, 29, tzinfo=timezone(timedelta(hours=2)))
    tomorrow_prices = []
    for i, price in enumerate(prices):
        tomorrow_prices.append(
            {
                "start": (base + timedelta(hours=i)).isoformat(),
                "end": (base + timedelta(hours=i + 1)).isoformat(),
                "price": price,
                "is_cheap": price < 0.7,
                "is_expensive": price > 1.2,
            }
        )
    return {
        "data_timestamp": "2026-04-28T20:55:57+02:00",
        "tomorrow_prices": tomorrow_prices,
        "average_price_tomorrow": 0.63,
    }


@pytest.fixture
def sample_expensive_day() -> dict[str, Any]:
    """Generate a day with uniformly high prices."""
    prices = [
        1.50,
        1.45,
        1.40,
        1.38,
        1.42,
        1.55,
        1.80,
        1.90,
        1.75,
        1.60,
        1.30,
        1.25,
        1.20,
        1.15,
        1.25,
        1.10,
        1.20,
        1.55,
        1.70,
        1.85,
        1.95,
        1.80,
        1.40,
        1.35,
    ]
    base = datetime(2026, 4, 29, tzinfo=timezone(timedelta(hours=2)))
    tomorrow_prices = []
    for i, price in enumerate(prices):
        tomorrow_prices.append(
            {
                "start": (base + timedelta(hours=i)).isoformat(),
                "end": (base + timedelta(hours=i + 1)).isoformat(),
                "price": price,
                "is_cheap": False,
                "is_expensive": True,
            }
        )
    return {
        "data_timestamp": "2026-04-28T20:55:57+02:00",
        "tomorrow_prices": tomorrow_prices,
        "average_price_tomorrow": 1.50,
    }


@pytest.fixture
def sample_battery_state() -> dict[str, Any]:
    """Generate a realistic battery state."""
    return {
        "state": "26.4",  # ~50% SoC for 8S LiFePO4
        "attributes": {
            "unit_of_measurement": "V",
            "device_class": "voltage",
            "friendly_name": "Gniazdo Battery Voltage",
        },
    }


@pytest.fixture
def sample_battery_current() -> dict[str, Any]:
    """Generate a realistic battery current state."""
    return {
        "state": "5.0",  # 5A discharging
        "attributes": {
            "unit_of_measurement": "A",
            "device_class": "current",
            "friendly_name": "Gniazdo Battery Discharge Current",
        },
    }


@pytest.fixture
def sample_load_power() -> dict[str, Any]:
    """Generate a realistic load power state."""
    return {
        "state": "0.15",  # 0.15W idle
        "attributes": {
            "friendly_name": "Gniazdo Output Active Power",
        },
    }


@pytest.fixture
def mock_hass_with_data(
    mock_hass: MagicMock,
    sample_battery_state: dict,
    sample_battery_current: dict,
    sample_load_power: dict,
) -> MagicMock:
    """Create a fully mocked HA instance with realistic sensor data."""

    def get_side_effect(entity_id):
        states_map = {
            "sensor.gniazdo_battery_voltage": State(
                entity_id="sensor.gniazdo_battery_voltage",
                state="26.4",
                attributes={"unit_of_measurement": "V"},
            ),
            "sensor.gniazdo_battery_discharge_current": State(
                entity_id="sensor.gniazdo_battery_discharge_current",
                state="5.0",
                attributes={"unit_of_measurement": "A"},
            ),
            "sensor.gniazdo_output_active_power": State(
                entity_id="sensor.gniazdo_output_active_power",
                state="0.15",
                attributes={},
            ),
        }
        return states_map.get(entity_id)

    mock_hass.states.get.side_effect = get_side_effect
    return mock_hass


@pytest.fixture
def mock_config_entry() -> MagicMock:
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "cvxpy_optimizer_001"
    return entry
