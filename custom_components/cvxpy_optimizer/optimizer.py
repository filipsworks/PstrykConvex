import cvxpy as cp
import numpy as np
import pandas as pd

class BatteryOptimizer:
    def __init__(self, capacity_kwh, max_charge_kw, max_discharge_kw, efficiency=0.95):
        self.capacity_kwh = capacity_kwh
        self.max_charge_kw = max_charge_kw
        self.max_discharge_kw = max_discharge_kw
        self.efficiency = efficiency

    def optimize(self, prices, loads, initial_soc_kwh):
        """
        prices: pd.Series with index as time intervals
        loads: pd.Series with index as time intervals
        initial_soc_k	: float (kWh)
        """
        n = len(prices)
        t = np.arange(n)
        
        # Decision Variables
        p_charge = cp.Variable(n, nonneg=True)
        p_discharge = cp.Variable(n, nonneg=True)
        p_grid = cp.Variable(n)
        soc = cp.Variable(n + 1, nonneg=True)

        # Parameters from input
        price_vec = prices.values
        load_vec = loads.values
        
        # Constraints
        constraints = []
        
        # Initial SoC
        constraints += [soc[0] == initial_soc_kwh]
        
        # Power Balance: P_grid + P_discharge = P_load + P_charge
        # (Assuming no solar for now)
        constraints += [p_grid + p_discharge == load_vec + p_charge]
        
        # Charging/Discharging limits
        constraints += [p_charge <= self.max_charge_kw]
        constraints += [p_discharge <= self.max_discharge_kw]
        
        # SoC Evolution (assuming 1 hour intervals)
        # soc[t+1] = soc[t] + (p_charge * eff - p_discharge / eff) * dt
        for i in range(n):
            constraints += [soc[i+1] == soc[i] + (p_charge[i] * self.efficiency - p_discharge[i] / self.efficiency)]

        # SoC bounds
        constraints += [soc <= self.capacity_kwh]
        
        # Objective: Minimize cost (price * p_grid)
        # Note: we only care about the cost of power from grid
        objective = cp.Minimize(cp.sum(cp.multiply(price_vec, p_grid)))

        prob = cp.Problem(objective, constraints)
        prob.solve()

        if prob.status not in ["optimal", "optimal_inaccurate"]:
            raise ValueError(f"Optimization failed with status: {numpy_status := prob.status}")

        # Results
        return pd.DataFrame({
            "p_charge": p_charge.value,
            "p_discharge": p_discharge.value,
            "p_grid": p_grid.value,
            "soc": soc.value[:-1] # exclude the last element which is soc[n]
        }, index=prices.index)

if __name__ == "__main__":
    # Quick Test
    optimizer = BatteryOptimizer(capacity_kwh=10, max_charge_kw=5, max_discharge_kw=5)
    test_prices = pd.Series([0.1, 0.5, 0.2, 0.8, 0.1], index=pd.date_range("2026-01-01", periods=5, freq="H"))
    test_loads = pd.Series([1, 1, 1, 1, 1], index=pd.date_range("2026-01-01", periods=5, freq="H"))
    
    try:
        results = optimizer.optimize(test_prices, test_loads, initial_soc_kwh=5)
        print(results)
    except Exception as e:
        print(f"Failed: {e}")
