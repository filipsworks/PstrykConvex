# CVXPY Optimizer for Home Assistant

This is a Home Assistant custom component that uses convex optimization to manage battery charging and discharging based on electricity prices.

## Features
- Uses `cvxpy` to solve the optimization problem.
- Fetches electricity prices and load profiles from Home Assistant sensors.
- Provides decisions for battery output mode and charger priority.

## Installation
1. Copy the `custom_components/cvxpy_optimizer` folder to your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration
The component uses existing Home Assistant sensors:
- `sensor.tomorrow_prices`: Must have an attribute `prices` containing a list of `{"start": "...", "price": ...}`.
- `sensor.inverter_power_history`: Must have an attribute `history` containing a list of `{"time": "...", "state": ...}`.
- `sensor.battery_voltage_history`: Must have an attribute `history` containing a list of `{"time": "...", "state": ...}`.

## Usage
You can trigger the optimization manually by calling the service:
`cvxpy_optimizer.run_optimization`
