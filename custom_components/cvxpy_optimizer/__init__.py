"""CVXPY Battery Optimizer – Home Assistant integration entry point.

Registers the optimize_battery service and manages the optimizer lifecycle.
Triggered hourly via automation to compute optimal battery charge/discharge modes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, SERVICE_OPTIMIZE_BATTERY
from .decider import ModeDecider, make_decision
from .fetcher import BatteryState, Fetcher, PriceData
from .optimizer import BatteryOptimizer, OptimizationResult

_LOGGER = logging.getLogger(__name__)


# ── Platform Setup ────────────────────────────────────────────────────────


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the CVXPY optimizer from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Register the optimize_battery service
    if not hass.services.has_service(DOMAIN, SERVICE_OPTIMIZE_BATTERY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_OPTIMIZE_BATTERY,
            handle_optimize_battery,
        )

    # Set up sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    _LOGGER.info("CVXPY Battery Optimizer setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok


# ── Service Handler ───────────────────────────────────────────────────────


async def handle_optimize_battery(call: ServiceCall) -> dict[str, Any]:
    """Handle the optimize_battery service call.

    This is the main entry point triggered by Home Assistant automations.
    Fetches current data, runs optimization, and returns recommended modes.

    Service call example:
      service: cvxpy_optimizer.optimize_battery
      data:
        initial_soc: 0.75  # Optional, defaults to sensor value
    """
    hass = call.hass
    domain_data = hass.data.setdefault(DOMAIN, {})

    _LOGGER.info("Running battery optimization...")

    try:
        fetcher = Fetcher(hass=hass)
        optimizer = BatteryOptimizer()
        decider = ModeDecider()

        # Fetch data concurrently
        price_task = asyncio.create_task(fetcher.fetch_horizon_prices())
        battery_state_task = asyncio.create_task(fetcher.fetch_battery_state())
        load_task = asyncio.create_task(fetcher.fetch_current_load())

        prices, battery_state, current_load = await asyncio.gather(
            price_task, battery_state_task, load_task
        )

        if prices is None:
            _LOGGER.error("Cannot optimize without price data")
            result = {"success": False, "error": "Price data unavailable"}
            domain_data["latest_result"] = result
            return result

        # Get initial SoC from service call or sensor
        initial_soc = call.data.get("initial_soc")
        if initial_soc is None and battery_state.soc is not None:
            initial_soc = battery_state.soc
        elif initial_soc is None:
            initial_soc = 0.5  # Default fallback

        # Build load array (use current load as baseline for all steps)
        n_steps = len(prices.prices)
        if current_load is not None:
            load_array = np.full(n_steps, current_load)
        else:
            load_array = np.zeros(n_steps)

        # Run optimization
        opt_result = optimizer.optimize(
            prices=prices,
            initial_soc=float(initial_soc),
            load_power=load_array,
        )

        if not opt_result.solver_status.startswith("optimal"):
            _LOGGER.warning("Optimization did not converge optimally: %s", opt_result.solver_status)

        # Make mode decision
        mode_decision = make_decision(
            prices=prices,
            battery_state=battery_state,
            opt_result=opt_result,
        )

        # Build response dict
        result = {
            "success": True,
            "discharge_mode": mode_decision.discharge_mode,
            "charger_mode": mode_decision.charger_mode,
            "reason": mode_decision.reason,
            "schedule_summary": mode_decision.schedule_summary,
            "solver_status": opt_result.solver_status,
        }

        # Store for sensor entities to read
        domain_data["latest_result"] = result

        _LOGGER.info(
            "Optimization complete: discharge=%s, charger=%s, cost=%.2f PLN",
            mode_decision.discharge_mode,
            mode_decision.charger_mode,
            opt_result.total_cost,
        )

        return result

    except Exception:
        _LOGGER.exception("Error during battery optimization")
        error_result = {
            "success": False,
            "error": "Internal error during optimization",
        }
        domain_data["latest_result"] = error_result
        return error_result
