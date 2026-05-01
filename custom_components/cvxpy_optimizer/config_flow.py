"""Configuration flow for the CVXPY Battery Optimizer.

Currently minimal since all configuration is sensor-based.
Future expansion may allow custom battery parameters and optimization preferences.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CVXPY Battery Optimizer."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle a user start."""
        if user_input is not None:
            return self.async_create_entry(title="CVXPY Optimizer", data=user_input)

        return self.async_show_form(
            step_id="user",
            description_placeholders={
                "domain": DOMAIN,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for CVXPY Battery Optimizer."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(step_id="init")
