import pytest
import pandas as pd
import pytest
import pandas as pd
from cvxpy_optimizer.optimizer import BatteryOptimizer

import pytest
import pandas as pd


def test_optimizer_respects_max_charge():
    # Setup optimizer with low charge limit
    capacity = 10.0
    max_charge = 1.0  # Only 1kW allowed
    max_discharge = 5.0
    optimizer = BatteryOptimizer(capacity_kwh=capacity, max_charge_kw=max_charge, max_discharge_kw=max_discharge)

    prices = pd.Series([0.1, 0.1], index=[0, 1])
    loads = pd.Series([2.0, 2.0], index=[0, 1])
    
    # Even if price is low, it shouldn't exceed 1kW charge
    results = optimizer.optimize(prices, loads, initial_soc_kwh=5.0)
    assert results["p_charge"].max() <= 1.0 + 1e-6

def test_optimizer_respects_capacity():
    optimizer = BatteryOptimizer(capacity_kwh=2.0, max_charge_kw=5.0, max_discharge_kw=5.0)
    prices = pd.Series([0.1, 0.1], index=[0, 1])
    loads = pd.Series([0.5, 0.5], index=[0, 1])
    
    # Start at 1.9kWh, charge for 2 hours (should hit limit)
    results = optimizer.optimize(prices, loads, initial_soc_kwh=1.9)
    assert results["soc"].max() <= 2.0 + 1e-6

def test_optimizer_empty_input():
    optimizer = BatteryOptimizer(capacity_kwh=10.0, max_charge_kw=5.0, max_discharge_kw=5.0)
    prices = pd.Series([], dtype=float)
    loads = pd.Series([], dtype=float)
    
    with pytest.raises((Exception, ValueError)): 
        optimizer.optimize(prices, loads, initial_soc_kwh=5.0)

