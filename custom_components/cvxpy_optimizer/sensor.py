"""Sensor entities for the CVXPY Battery Optimizer.

Provides output sensors that display the recommended operating modes:
  - sensor.battery_optimizer_discharge_mode (SUB/SBU)
  - sensor.battery_optimizer_charger_mode (CSO/SNU/OSO)
  - sensor.battery_optimizer_recommended_soc (percentage)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SERVICE_OPTIMIZE_BATTERY,
)

_LOGGER = logging.getLogger(__name__)


class BatteryOptimizerSensor(Entity):
    """Base sensor entity for optimizer output."""

    _attr_should_poll = True
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        sensor_type: str,
        name: str,
        icon: str,
    ):
        """Initialize the sensor."""
        self._hass = hass
        self._config_entry = config_entry
        self._sensor_type = sensor_type
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{sensor_type}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": "Battery Optimizer",
            "manufacturer": "Custom",
            "model": "CVXPY Optimizer v1",
        }
        self._state: Optional[str] = None
        self._attr_native_value = None

    @property
    def state(self) -> Optional[str]:
        return self._state

    async def async_update(self) -> None:
        """Update the sensor state from the last optimization result."""
        # The state is updated when the optimize_battery service is called.
        # We store results in hass.data for cross-entity access.
        domain_data = self._hass.data.get(DOMAIN, {})
        latest_result = domain_data.get("latest_result")

        if latest_result:
            if self._sensor_type == "battery_optimizer_discharge_mode":
                self._state = latest_result.get("discharge_mode")
                self._attr_native_value = self._state
            elif self._sensor_type == "battery_optimizer_charger_mode":
                self._state = latest_result.get("charger_mode")
                self._attr_native_value = self._state
            elif self._sensor_type == "battery_optimizer_recommended_soc":
                summary = latest_result.get("schedule_summary", {})
                soc_pct = summary.get("final_soc_pct")
                if soc_pct is not None:
                    self._state = f"{soc_pct}%"
                    self._attr_native_value = soc_pct

    async def async_added_to_hass(self) -> None:
        """Register callbacks for state updates."""

        # Listen for service calls that update optimization results
        @callback
        def _handle_service_call(call) -> None:
            if call.service == SERVICE_OPTIMIZE_BATTERY and call.data.get("success"):
                self.hass.data.setdefault(DOMAIN, {})["latest_result"] = call.data
                self.async_schedule_update_ha_state()

        self._unsub = self._hass.services.async_register(
            DOMAIN, SERVICE_OPTIMIZE_BATTERY, _handle_service_call
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup."""
        if hasattr(self, "_unsub"):
            self._unsub()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the optimizer output sensors."""
    from . import BatteryOptimizerSensor  # noqa: F811

    sensors = [
        BatteryOptimizerSensor(
            hass=hass,
            config_entry=config_entry,
            sensor_type="battery_optimizer_discharge_mode",
            name="Discharge Mode",
            icon="mdi:battery-arrow-up",
        ),
        BatteryOptimizerSensor(
            hass=hass,
            config_entry=config_entry,
            sensor_type="battery_optimizer_charger_mode",
            name="Charger Mode",
            icon="mdi:battery-charging-high",
        ),
        BatteryOptimizerSensor(
            hass=hass,
            config_entry=config_entry,
            sensor_type="battery_optimizer_recommended_soc",
            name="Recommended SoC",
            icon="mdi:battery-50",
        ),
    ]
    async_add_entities(sensors)
