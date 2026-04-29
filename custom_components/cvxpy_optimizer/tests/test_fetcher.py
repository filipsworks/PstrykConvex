import pytest
import pandas as pd
from unittest.mock import MagicMock
from cvxpy_optimizer.fetcher import HomeAssistantFetcher

@pytest.fixture
def mock_hass():
    hass = MagicMock()
    return hass

def test_fetch_tomorrow_prices_success(mock_hass):
    mock_state = MagicMock()
    mock_state.attributes = {
        'prices': [
            {"start": "2026-04-29T00:00:00+02:00", "price": 0.81},
            {"start": "to_be_fixed", "price": 0.79}
        ]
    }
    mock_hass.states.get.return_value = mock_state
    
    fetcher = HomeAssistantFetcher(mock_hass)
    prices = fetcher.fetch_tomorrow_prices()
    assert len(prices) == 2
    assert prices.iloc[0]["price"] == 0.81

def test_fetch_tomorrow_prices_empty(mock_hass):
    mock_state = MagicMock()
    mock_state.attributes = {}
    mock_hass.states.get.return_value = mock_state
    
    fetcher = HomeAssistantFetcher(mock_hass)
    prices = fetcher.fetch_tomorrow_prices()
    assert prices.empty

def test_fetch_load_profile_success(mock_hass):
    mock_state = MagicMock()
    mock_state.attributes = {
        'history': [
            {'time': '2026-04-29T00:00:00', 'state': 1.5},
            {'time': '2026-04-29T01:00:00', 'state': 1.2}
        ]
    }
    mock_hass.states.get.return_value = mock_state
    
    fetcher = HomeAssistantFetcher(mock_hass)
    load = fetcher.fetch_load_profile()
    assert len(load) == 2
    assert load.iloc[0]['state'] == 1.5

def test_fetch_load_profile_malformed(mock_hass):
    mock_state = MagicMock()
    mock_state.attributes = {'history': 'not a list'}
    mock_hass.states.get.return_value = mock_state
    
    fetcher = HomeAssistantFetcher(mock_hass)
    load = fetcher.fetch_load_profile()
    assert load.empty

def test_fetch_battery_status_success(mock_hass):
    mock_state = MagicMock()
    mock_state.attributes = {
        'history': [
            {'time': '2026-04-29T00:00:00', 'state': 25.5},
            {'time': '2026-04-29T01:00:00', 'state': 26.0}
        ]
    }
    mock_hass.states.get.return_value = mock_state
    
    fetcher = HomeAssistantFetcher(mock_hass)
    battery = fetcher.fetch_battery_status()
    assert len(battery) == 2
    assert battery.iloc[0]['state'] == 25.5
