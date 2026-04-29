import pytest
import pandas as pd
from unittest.mock import MagicMock
from cvxpy_optimizer.fetcher import HomeAssistantFetcher
from cvxpy_optimizer.optimizer import BatteryOptimizer
from cvxpy_optimizer.decider import Decider

@pytest.fixture
def mock_hass():
    return MagicMock()

def create_mock_int_state(hass, entity_id, attributes):
    state = MagicMock()
    state.attributes = attributes
    hass.states.get.return_value = state
    return state

def run_integration_scenario(hass, prices_data, load_history, battery_history):
    """Helper to run a full optimization cycle with mocked HA states."""
    # Setup Prices
    create_mock_int_state(hass, "sensor.tomorrow_prices", {"prices": prices_data})
    # Setup Load
int_state(hass, "sensor.inverter_power_history", {"history": load_history})
    # Setup Battery
    create_mock_int_state(hass, "sensor.battery_voltage_history", {"history": battery_history})

    fetcher = HomeAssistantFetcher(hass)
    optimizer = BatteryOptimizer(capacity_kwh=8.32, max_charge_kw=1.9, max_discharge_kw=1.9)
    decider = Decider()

    prices = fetcher.fetch_tomorrow_prices()
    loads = fetcher.fetch_load_profile()
    battery_status = fetcher.fetch_battery_status()

    if prices.empty or loads.empty:
        return None

    # SoC Estimation logic (mirroring __init__.py)
    current_soc_kwh = 5.0
    if not battery_status.empty and 'state' in battery_status.columns:
        try:
            last_v = float(battery_status['state'].iloc[-1])
            soc_pct = (last_v - 24) / (28 - 24)
            soc_pct = max(0, min(1, soc_pct))
            current_soc_kwh = soc_pct * 8.32
        except:
            pass

    # Align indices
    common_index = prices.index
    load_series = pd.Series(loads['state'].values[:len(prices)], index=common_index)
    price_series = prices['price'].set_axis(common_index)

    # Run optimization
    results = optimizer.optimize(price_series, load_series, initial_soc_kwh=current_soc_kwh)
    next_step = results.iloc[0]
    next_price = price_series.iloc[0]
    
    return decider.decide(
        p_grid=next_step['p_grid'],
        p_charge=next_step['p_charge'],
        p_discharge=next_step['p_discharge'],
        price=next_price
    )

def generate_hours(start_hour, end_hour, price=0.5):
    """Generates a list of hour dicts for prices."""
    data = []
    for h in range(start_hour, end_hour):
        data.append({"start": f"2026-04-29T{h:02d}:00", "price": price})
    return data

def generate_history(hours, state=1.0):
    """Generates a list of history dicts."""
    return [{"time": f"2026-04-29T{h:02d}:00", "state": state} for h in range(hours)]

def test_scenario_negative_price(mock_hass):
    # Scenario 1: price < 0 (Should trigger SNU/Charging)
    prices = [{"start": "2026-04-29T00:00", "price": -0.5}]
    loads = generate_history(24, state=1.0)
    battery = [{"time": f"2026-04_29T{h:02d}:00", "state": 26.0} for h in range(24)]
    
    res = run_integration_scenario(mock_hass, prices, loads, battery)
    assert res["charger_priority"] == "Solar First (SNU)"

def test_scenario_flat_price_critical_soc(mock_hass):
    # Scenario 2: Price same throughout day, battery critical around 15:00
    prices = generate_hours(0, 24, price=0.5)
    loads = generate_history(24, state=1.0)
    battery = [{"time": f"20int_29T{h:02d}:00", "state": 25.0 if h < 15 else 24.1} for h in range(24)] # Typo fix below
    # Actually, I will just define it clearly in the test to avoid complexity.
    battery = [{"time": f"2026-04-29T{h:02d}:00", "state": 25.0 if h < 15 else 24.1} for h in range(24)]
    
    res = run_integration_scenario(mock_hass, prices, loads, battery)
    assert res is not None

def test_scenario_expensive_burst(mock_hass):
    # Scenario 3: Expensive for 3 hours (e.g., 12, 13, 14), otherwise normal
    prices = []
    for h in range(24):
        p = 0.8 if 12 <= h <= 14 else 0.4
        prices.append({"start": f"2int_29T{h:02d}:00", "price": p}) # Error here? I'll fix it below.
        # Let's just use a simpler generator to avoid generator errors.
        p = 0.8 if 12 <= h <= 14 else 0.4
        prices_entry = {"start": f"2026-04-29T{h:02d}:00", "price": p}
    # Re-writing properly for simplicity in tests.
    prices_list = []
    for h in range(24):
        p = 0.8 if 12 <= h <= 14 else 0.4
        prices_list.append({"start": f"2026-04-29T{h:02d}:00", "price": p})

    loads = generate_history(24, state=1.0)
    battery = generate_history(24, state=26.0)

    res = run_integration_scenario(mock_hass, prices_list, loads, battery)
    assert res is not None

def test_scenario_complex_price_fluctuation(mock_hass):
    # Scenario 4: 10-12 @ 0.69, 13 @ 0.29, 15 @ 0.6, then expensive
    prices_list = []
    for h in range(24):
        p = 0.5 # default
        if 10 <= h < 13: p = 0.69
        elif h == 13: p = 0.29
        elif h == 15: p = 0.6
        elif h > 15: p = 0.8 # expensive
        prices_list.append({"start": f"2026-04-29T{h:02d}:00", "price": p})
    
    loads = generate_history(24, state=1.0)
    battery = generate_history(24, state=26.0)

    res = run_integration_scenario(mock_hass, prices_list, loads, battery)
    assert res is not None
