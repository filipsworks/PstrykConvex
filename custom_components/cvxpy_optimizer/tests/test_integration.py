import pytest
import pandas as pd
from unittest.mock import MagicMock
from cvxpy_optimizer.fetcher import HomeAssistantFetcher
from cvxpy_optimizer.optimizer import BatteryOptimizer
from cvxpy_optimizer.decider import Decider

@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass._mock_states = {}
    def get_state(eid):
        return hass._mock_states.get(eid)
    hass.states.get.side_effect = get_state
    return hass

def create_mock_int_state(hass, entity_id, attributes):
    state = MagicMock()
    state.attributes = attributes
    hass._mock_states[entity_id] = state
    return state

def run_integration_scenario(hass, prices_data, loads_data, battery_data):
    create_mock_int_state(hass, "sensor.tomorrow_prices", {"prices": prices_data})
    create_mock_int_state(hass, "sensor.inverter_power_history", {"history": loads_data})
    create_mock_int_state(hass, "sensor.battery_voltage_history", {"history": battery_data})

    fetcher = HomeAssistantFetcher(hass)
    optimizer = BatteryOptimizer(capacity_kwh=8.32, max_charge_kw=1.9, max_discharge_kw=3.6)
    decider = Decider()

    prices_df = fetcher.fetch_tomorrow_prices()
    loads_df = fetcher.fetch_load_profile()
    battery_status_df = fetcher.fetch_battery_status()

    if prices_df.empty or loads_df.empty:
        return []

    # Convert 'start' to datetime index for prices
    prices_df['start'] = pd.to_datetime(prices_df['start'])
    prices_df.set_index('start', inplace=True)
    price_series = prices_df['price']

    # Convert 'time' to datetime index for loads and align with prices
    loads_df['time'] = pd.to_datetime(loads_df['time'])
    loads_df.set_index('time', inplace=True)
    load_series = loads_df['state'].reindex(price_series.index, method='ffill').fillna(0)

    # Determine initial SoC from battery history
    initial_soc_kwh = 4.16
    if not battery_status_df.empty and 'state' in battery_status_df.columns:
        try:
            last_v = float(battery_status_df['state'].iloc[-1])
            soc_pct = (last_v - 24) / (28 - 24)
            soc_pct = max(0, min(1, soc_pct))
            initial_soc_kwh = soc_pct * 8.32
        except:
            pass

    results = optimizer.optimize(price_series, load_series, initial_soc_kwh=initial_soc_kwh)
    
    decisions = []
    for i in range(len(results)):
        next_step = results.iloc[i]
        price_val = price_series.iloc[i]
        decisions.append(decider.decide(
            p_grid=next_step['p_grid'],
            p_charge=next_step['p_charge'],
            p_discharge=next_step['p_discharge'],
            price=price_val
        ))
    return decisions

def test_scenario_negative_price(mock_hass):
    prices = [
        {"start": "2026-04-29T00:00", "price": -0.5},
        {"start": "20int_29T01:00", "price": 0.5} # Typo...
    ]
    # Fixed correctly
    prices = [
        {"start": "2026-04-29T00:00", "price": -0.5},
        {"start": "2026-04-29T01:00", "price": 0.5}
    ]
    loads = [
        {"time": "2026-04-29T00:00", "state": 1.0},
        {"time": "2026-04-29T01:00", "state": 1.0}
    ]
    battery = [
        {"time": "2026-04-29T00:00", "state": 26.0}
    ]
    decisions = run_integration_scenario(mock_hass, prices, loads, battery)
    assert len(decisions) > 0
    assert decisions[0]["charger_priority"] == "Solar First (SNU)"

def test_scenario_flat_price_critical_soc(mock_hass):
    prices = [
        {"start": "2026-04-29T00:00", "price": 0.5},
        {"start": "2026-04-29T01:00", "price": 0.5}
    ]
    loads = [
        {"time": "2026-04-29T00:00", "state": 1.0},
        {"time": "2026-04-29T01:00", "state": 1.0}
    ]
    battery = [
        {"time": "2026-04-29T00:00", "state": 24.5}
    ]
    decisions = run_integration_scenario(mock_hass, prices, loads, battery)
    assert len(decisions) > 0
    assert decisions[0]["charger_priority"] in ["Solar Only (OSO)", "Solar + Utility (CSO)"]

def test_scenario_expensive_burst(mock_hass):
    prices = [
        {"start": "2026-04-29T00:00", "price": 0.1},
        {"start": "2026-04-29T01:00", "price": 1.5},
        {"start": "2026-04-29T02:00", "price": 0.1}
    ]
    loads = [
        {"time": "2026-04-29T00:00", "state": 1.0},
        {"time": "2026-04-29T01:00", "state": 1.0},
        {"time": "2026-04_29T02:00", "state": 1.0} # Typo...
    ]
    # Fixed correctly
    loads = [
        {"time": "2026-04-29T00:00", "state": 1.0},
        {"time": "2026-04-29T01:00", "state": 1.0},
        {"time": "2026-04-29T02:00", "state": 1.0}
    ]
    battery = [
        {"time": "2026-04-29T00:00", "state": 26.0},
        {"time": "2026-04-29T01:00", "state": 26.0},
        {"time": "2026-04-29T02:00", "state": 26.0}
    ]
    decisions = run_integration_scenario(mock_hass, prices, loads, battery)
    assert len(decisions) > 0
    assert decisions[1]["output_mode"] == "Solar+Battery First (SBU)"

def test_scenario_complex_fluctuation(mock_hass):
    prices = [
        {"start": "2026-04-29T00:00", "price": 0.1},
        {"start": "2026-04-29T01:00", "price": 0.8},
        {"start": "2026-04-29T02:00", "price": 0.2},
        {"start": "2026-04-29T03:00", "price": 1.2}
    ]
    loads = [
        {"time": "20int_29T00:00", "state": 1.0}, # Typo...
        {"time": "2026-04-29T01:00", "state": 1.5},
        {"time": "2026-04-29T02:00", "state": 0.5},
        {"time": "2026-04-29T03:00", "state": 2.0}
    ]
    # Fixed correctly
    loads = [
        {"time": "2026-04-29T00:00", "state": 1.0},
        {"time": "2026-04-29T01:00", "state": 1.5},
        {"time": "2026-04-29T02:00", "state": 0.5},
        {"time": "2026-04-29T03:00", "state": 2.0}
    ]
    battery = [
        {"time": "2026-04-29T00:00", "state": 26.0},
        {"time": "2026-04-29T01:00", "state": 26.0},
        {"time": "2026-04-29T02:00", "state": 26.0},
        {"time": "2026-04-29T03:00", "state": 26.0}
    ]
    decisions = run_integration_scenario(mock_hass, prices, loads, battery)
    assert len(decisions) > 0
