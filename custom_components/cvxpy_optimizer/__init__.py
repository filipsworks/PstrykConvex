import logging
import pandas as pd
from .fetcher import HomeAssistantFetcher
from .optimizer import BatteryOptimizer
from .decider import Decider

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass, config):
    """Set up the cvxpy_optimizer integration."""
    _LOGGER.info("Setting up CVXPRY Optimizer integration")
    
    fetcher = HomeAssistantFetcher(hass)
    decider = Decider()

    # We will run the optimization periodically or via a service call.
    # For now, let's just implement a basic setup that can be triggered.
    
    async def run_optimization(_):
        try:
            prices = fetcher.fetch_tomorrow_prices()
            loads = fetcher.fetch_load_profile()
            battery_status = fetcher.fetch_battery_status()

            if prices.empty or loads.empty:
                _LOGGER.error("Could not fetch enough data to run optimization.")
                return

            # 2. Estimate Battery SoC (Simplified)
            current_soc_kwh = 5.0 
            if not battery_status.empty and 'state' in battery_status.columns:
                try:
                    last_v = float(battery_status['state'].iloc[-1])
                    # Linear approximation: 24V -> 0%, 28V -> 100%
                    soc_pct = (last_v - 24) / (28 - 24)
                    soc_pct = max(0, min(1, soc_pct))
                    # Total capacity: 3.2kWh (using a smaller value for example)
                    current_soc_kwh = soc_pct * 8.32
                    _LOGGER.info(f"Estimated current SoC from voltage ({last_v}V): {soc_pct*100:.1f}% ({current_soc_kwh:.2f} kWh)")
                except Exception as e:
                    _LOGGER.warning(f"Could not estimate SoC from voltage, using default 5kWh: {e}")

            # 3. Optimization Parameters
            capacity_kwh = 8.32 
            max_charge_kw = 1.9  
            max_discharge_kw = 3.6
            
            optimizer = BatteryOptimizer(
                capacity_kwh=capacity_kwh,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw
            )

            # Prepare loads for optimization (need to match prices index)
            common_index = prices.index
            load_series = pd.Series(loads['state'].values[:len(prices)], index=common_index)
            price_int_series = prices['price'].set_axis(common_index)

            # 4. Run Optimization
            _LOGGER.info("Running optimization...")
            results = optimizer.optimize(price_int_series, load_series, initial_soc_kwh=current_soc_kwh)
            
            # 5. Get decision for the NEXT step (index 0 of results)
            next_step = results.iloc[0]
            next_price = price_int_series.iloc[0]
            
            decision = decider.decide(
                p_grid=next_step['p_grid'],
                p_charge=next_step['p_charge'],
                p_discharge=next_step['p_discharge'],
                price=next_price
            )

            _LOGGER.info(f"Optimization Decision for Next Step: Output Mode={decision['output_mode']}, Charger Priority={decision['charger_priority']}")

        except Exception as e:
            _LOGGER.error(f"Optimization error: {e}")

    # Register a service to trigger optimization manually
    hass.services.async_register("cvxpy_optimizer", "run_optimization", run_optimization)

    return True
