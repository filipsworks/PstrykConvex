"""Data fetcher for the CVXPY Battery Optimizer integration.

Retrieves pricing, load profile, and battery status data from Home Assistant
using hass.states and HA history/recorder helpers instead of direct HTTP calls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
from homeassistant.core import HomeAssistant, State
from homeassistant.util.dt import as_utc, utc_from_timestamp

from .const import (
    ATTR_END,
    ATTR_PRICE,
    ATTR_START,
    ATTR_TODAY_PRICES,
    ATTR_TOMORROW_PRICES,
    HORIZON_HOURS,
    SENSOR_BATTERY_CURRENT,
    SENSOR_BATTERY_VOLTAGE,
    SENSOR_LOAD_POWER,
    SENSOR_TODAY_PRICES,
    SENSOR_TOMORROW_PRICES,
)

_LOGGER = logging.getLogger(__name__)


class PriceData:
    """Container for price data over an optimization horizon."""

    def __init__(self, timestamps: list[datetime], prices: list[float]):
        self.timestamps = timestamps
        self.prices = np.array(prices, dtype=np.float64)

    @property
    def has_negative_prices(self) -> bool:
        """Return True if any price in the horizon is negative."""
        return np.any(self.prices < 0)

    @property
    def min_price(self) -> float:
        return float(np.min(self.prices))

    @property
    def max_price(self) -> float:
        return float(np.max(self.prices))

    @property
    def avg_price(self) -> float:
        return float(np.mean(self.prices))


class BatteryState:
    """Container for current battery state information."""

    def __init__(
        self,
        voltage: Optional[float] = None,
        current: Optional[float] = None,
        soc: Optional[float] = None,
    ):
        self.voltage = voltage
        self.current = current  # Positive = discharging, Negative = charging
        self.soc = soc  # Estimated SoC (0.0 – 1.0)

    @property
    def is_available(self) -> bool:
        return self.voltage is not None


class Fetcher:
    """Fetches sensor data from Home Assistant for the optimizer."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    # ── Price Data ───────────────────────────────────────────────────────

    async def fetch_today_prices(self) -> Optional[PriceData]:
        """Fetch today's hourly electricity prices from HA.

        Today prices are always available via sensor.pstryk_aio_obecna_cena_zakupu_pradu.
        Returns PriceData with 24 hourly timestamps and prices, or None on failure.
        """
        return await self._fetch_prices_from_sensor(SENSOR_TODAY_PRICES, ATTR_TODAY_PRICES)

    async def fetch_tomorrow_prices(self) -> Optional[PriceData]:
        """Fetch tomorrow's hourly electricity prices from HA.

        Tomorrow prices are only available between 13:00–19:00.
        Returns PriceData with 24 hourly timestamps and prices, or None if unavailable.
        """
        return await self._fetch_prices_from_sensor(SENSOR_TOMORROW_PRICES, ATTR_TOMORROW_PRICES)

    async def fetch_horizon_prices(self) -> Optional[PriceData]:
        """Fetch price data for the optimization horizon.

        Prefers tomorrow's prices if available and we're past midday,
        otherwise falls back to today's prices. This ensures the optimizer
        always has forward-looking pricing data when possible.
        """
        now = datetime.now(timezone.utc)

        # If it's after ~13:00 UTC, tomorrow prices should be available
        if now.hour >= 11:
            tomorrow_prices = await self.fetch_tomorrow_prices()
            if tomorrow_prices is not None:
                _LOGGER.debug("Using tomorrow's prices for optimization horizon")
                return tomorrow_prices

        # Fall back to today's prices
        _LOGGER.debug("Tomorrow prices unavailable, using today's prices")
        return await self.fetch_today_prices()

    async def _fetch_prices_from_sensor(
        self, sensor_entity_id: str, prices_attr: str
    ) -> Optional[PriceData]:
        """Generic price fetching from a sensor with a prices attribute."""
        state = self.hass.states.get(sensor_entity_id)
        if state is None:
            _LOGGER.warning("Price sensor %s not found", sensor_entity_id)
            return None

        attributes = state.attributes or {}
        price_list = attributes.get(prices_attr)
        if not price_list:
            _LOGGER.warning(
                "No %s attribute in sensor %s (state=%s)",
                prices_attr,
                sensor_entity_id,
                state.state,
            )
            return None

        timestamps = []
        prices = []
        for entry in price_list:
            start_str = entry.get(ATTR_START)
            price_val = entry.get(ATTR_PRICE)
            if start_str is not None and price_val is not None:
                try:
                    ts = datetime.fromisoformat(start_str)
                    # Ensure timezone-aware
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    timestamps.append(ts)
                    prices.append(float(price_val))
                except (ValueError, TypeError) as exc:
                    _LOGGER.warning("Failed to parse price entry %s: %s", entry, exc)

        if not timestamps:
            _LOGGER.error("No valid price entries found in sensor %s", sensor_entity_id)
            return None

        return PriceData(timestamps=timestamps, prices=prices)

    # ── Load Profile ─────────────────────────────────────────────────────

    async def fetch_current_load(self) -> Optional[float]:
        """Fetch current active power load from HA.

        Returns the current value of sensor.gniazdo_output_active_power in Watts,
        or None if unavailable.
        """
        state = self.hass.states.get(SENSOR_LOAD_POWER)
        if state is None:
            _LOGGER.warning("Load sensor %s not found", SENSOR_LOAD_POWER)
            return None

        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.error("Invalid load value in sensor %s: %s", SENSOR_LOAD_POWER, state.state)
            return None

    async def fetch_historical_load(self, hours: int = 24) -> Optional[pd.Series]:
        """Fetch historical load data for forecasting.

        Uses HA's history endpoint to get past load readings.
        Returns a pandas Series indexed by timestamp with power values in Watts.
        """
        # Note: In actual HA integration, use hass.helpers.history.async_get_significant_states
        # For now, return None — the optimizer can work without historical data
        _LOGGER.debug("Historical load fetching for %d hours (placeholder)", hours)
        return None

    # ── Battery State ────────────────────────────────────────────────────

    async def fetch_battery_state(self) -> BatteryState:
        """Fetch current battery state from HA sensors.

        Reads sensor.gniazdo_battery_voltage and sensor.gniazdo_battery_discharge_current,
        then estimates SoC using the LiFePO4 voltage-SOC curve.
        """
        voltage = await self._fetch_float_sensor(SENSOR_BATTERY_VOLTAGE)
        current = await self._fetch_float_sensor(SENSOR_BATTERY_CURRENT)

        soc = None
        if voltage is not None:
            soc = self._voltage_to_soc(voltage)

        return BatteryState(voltage=voltage, current=current, soc=soc)

    async def fetch_historical_battery_current(
        self, hours: int = 24
    ) -> Optional[list[tuple[datetime, float]]]:
        """Fetch historical battery discharge current for SoC tracking.

        Returns a list of (timestamp, current) tuples where positive values
        indicate discharging and negative values indicate charging.
        """
        _LOGGER.debug("Historical battery current fetching for %d hours (placeholder)", hours)
        return None

    # ── SoC Estimation ───────────────────────────────────────────────────

    @staticmethod
    def _voltage_to_soc(voltage: float) -> float:
        """Estimate State of Charge from battery voltage for 8S LiFePO4.

        Uses a piecewise linear approximation of the LiFePO4 discharge curve:
          - 24.0V → 5% SOC (minimum, protects cells)
          - 25.6V → 20% SOC
          - 27.2V → 80% SOC
          - 28.0V → 100% SOC

        Args:
            voltage: Battery pack voltage in Volts (24–28V range).

        Returns:
            Estimated SoC as a float between 0.0 and 1.0.
        """
        from .const import (
            BATTERY_MAX_VOLTAGE,
            BATTERY_MIN_VOLTAGE,
            SOC_MAX,
            SOC_MIN,
        )

        # Clamp voltage to valid range
        v = max(BATTERY_MIN_VOLTAGE, min(BATTERY_MAX_VOLTAGE, voltage))

        # Piecewise linear LiFePO4 voltage-SOC curve
        points = [
            (24.0, 0.05),  # 5% SOC — cell protection floor
            (25.6, 0.20),  # 20% SOC
            (27.2, 0.80),  # 80% SOC
            (28.0, 1.00),  # 100% SOC — fully charged
        ]

        # If at or below minimum voltage
        if v <= points[0][0]:
            return SOC_MIN

        # If at or above maximum voltage
        if v >= points[-1][0]:
            return SOC_MAX

        # Find the segment and interpolate
        for i in range(len(points) - 1):
            v_lo, soc_lo = points[i]
            v_hi, soc_hi = points[i + 1]
            if v_lo <= v <= v_hi:
                if v_hi == v_lo:
                    return soc_lo
                frac = (v - v_lo) / (v_hi - v_lo)
                return soc_lo + frac * (soc_hi - soc_lo)

        return SOC_MIN  # Fallback

    async def _fetch_float_sensor(self, entity_id: str) -> Optional[float]:
        """Fetch a numeric value from an HA sensor."""
        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.warning("Sensor %s not found", entity_id)
            return None

        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.error("Invalid numeric value in sensor %s: %s", entity_id, state.state)
            return None


async def async_setup_fetcher(hass: HomeAssistant) -> Fetcher:
    """Factory function to create and return a Fetcher instance."""
    return Fetcher(hass=hass)
