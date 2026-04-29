import pytest
import pandas as pd

@pytest.fixture
def mock_prices():
    return pd.DataFrame({
        "start": ["2026-04-29T00:00:00+02:00", "2026-04_29T01:00:00+02:00"],
        "price": [0.8, -0.1]
    })

@pytest.fixture
def mock_loads():
    return pd.DataFrame({
        "state": [0.5, 0.6],
        "entity_id": ["sensor.a", "sensor.b"]
    })

@pytest.fixture
def mock_battery_status():
    return pd.DataFrame({
        "state": [26.3, 26.2],
        "entity_id": ["sensor.v", "sensor.v"]
    })
