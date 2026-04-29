import pandas as pd

class HomeAssistantFetcher:
    def __init__(self, hass):
        self.hass = hass

    def fetch_tomorrow_prices(self, entity_id="sensor.tomorrow_prices"):
        state = self.hass.states.get(entity_id)
        if not state or not state.attributes:
            return pd.DataFrame()
        
        prices_data = state.attributes.get('prices', [])
        if not prices_data:
            return pd.DataFrame()

        return pd.DataFrame(prices_data)

    def fetch_load_profile(self, entity_id="sensor.inverter_power_history"):
        state = self.hass.states.get(entity_id)
        if not state or not state.attributes:
            return pd.DataFrame()
        
        history_data = state.attributes.get('history', [])
        if not isinstance(history_data, list):
            return pd.DataFrame()

        if not history_data:
            return pd.DataFrame()

        return pd.DataFrame(history_data)

    def fetch_battery_status(self, entity_id="sensor.battery_voltage_history"):
        state = self.hass.states.get(entity_id)
        if not state or not state.attributes:
            return pd.DataFrame()
        
        history_data = state.attributes.get('history', [])
        if not history_data:
            return pd.DataFrame()

        return pd.DataFrame(history_data)
