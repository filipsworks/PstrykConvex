# CVXPY Battery Optimizer – Home Assistant Custom Integration

A robust Home Assistant custom integration that uses **CVXPY** (convex optimization) to determine optimal battery charging and discharging strategies based on dynamic electricity pricing.

## Overview

The `cvxpy_optimizer` integration minimizes total electricity costs by solving a linear programming problem over a 24-hour horizon. It considers:

- **Dynamic energy prices** (today's and tomorrow's hourly rates from Pstryk AIO)
- **Battery physical limits** (voltage, current, SoC bounds for 8S LiFePO4)
- **Load profile** (current household consumption)
- **Round-trip efficiency losses**

The integration outputs recommended operating modes that can be consumed by Home Assistant automations to control your inverter/charger.

## Battery Configuration

| Parameter | Value |
|---|---|
| Chemistry | LiFePO4 (3.2V per cell) |
| Configuration | 8S |
| Operating Voltage | 24V – 28V |
| Capacity | 320 Ah |
| Max Charge/Discharge Current | 70 A |
| Max Power | ~1680 W |
| Round-trip Efficiency | 95% |

## Output Modes

### Discharge Modes (Battery Usage)

| Mode | Name | Description |
|---|---|---|
| **SUB** | Solar First | Utility + Solar priority, battery as backup |
| **SBU** | Solar + Battery First | Battery and solar prioritized over grid |

### Charger Modes (Battery Charging)

| Mode | Name | Description |
|---|---|---|
| **CSO** | Charge Solar + Utility | Allows charging from both solar and grid |
| **SNU** | Solar + Negative Utility | Forced arbitrage when price < 0 (get paid to charge) |
| **OSO** | Solar Only | Charges only from solar, never from grid |

**Rule:** If any price in the optimization horizon is negative, charger mode automatically switches to SNU.

## Installation

### Step 1: Copy the Integration

Place the integration directory into your Home Assistant `custom_components` folder:

```bash
# On your HA system (via SSH/Samba/Configurator)
mkdir -p /config/custom_components/cvxpy_optimizer
cp -r <this_repo>/custom_components/cvxpy_optimizer/* /config/custom_components/cvxpy_optimizer/
```

### Step 2: Install Python Dependencies

The integration requires `cvxpy`, `numpy`, and `pandas`. These are listed in `manifest.json` and will be installed automatically by Home Assistant on first run (HA Core 2023.6+).

For local development/testing, install from `requirements.txt`:

```bash
pip install -r custom_components/cvxpy_optimizer/requirements.txt
```

### Step 3: Restart Home Assistant

Restart HA to load the new integration.

### Step 4: Add Integration (Optional)

Go to **Settings → Devices & Services → Add Integration** and search for "CVXPY Battery Optimizer". The integration is sensor-based and auto-discovers required entities.

## Required Sensors

The integration reads the following sensors from your Home Assistant instance:

| Sensor Entity ID | Purpose |
|---|---|
| `sensor.pstryk_aio_obecna_cena_zakupu_pradu` | Today's hourly electricity prices (always available) |
| `sensor.pstryk_aio_cena_zakupu_pradu_jutro` | Tomorrow's hourly prices (available 13:00–19:00) |
| `sensor.gniazdo_output_active_power` | Current household load in Watts |
| `sensor.gniazdo_battery_voltage` | Battery pack voltage (24–28V range) |
| `sensor.gniazdo_battery_discharge_current` | Battery current (positive = discharging) |

## Usage

### Service Call: `cvxpy_optimizer.optimize_battery`

Trigger the optimizer manually or via automation:

```yaml
service: cvxpy_optimizer.optimize_battery
data:
  initial_soc: 0.75  # Optional override for battery SoC (0.0–1.0)
```

**Response:**

```json
{
  "success": true,
  "discharge_mode": "SBU",
  "charger_mode": "CSO",
  "reason": "SoC=45%; price_range=[0.260, 1.640]; discharging to avoid high grid prices; allowing grid charging during cheap periods",
  "schedule_summary": {
    "total_steps": 24,
    "total_charge_wh": 3200.0,
    "total_discharge_wh": 2800.0,
    "estimated_cost_pln": 18.45,
    "initial_soc_pct": 45.0,
    "final_soc_pct": 62.0
  },
  "solver_status": "optimal"
}
```

### Hourly Automation Example

```yaml
alias: "Battery Optimizer – Hourly"
trigger:
  - platform: time_pattern
    hours: "/1"
condition: []
action:
  - service: cvxpy_optimizer.optimize_battery
    response_variable: !extend result
  - variables:
      discharge_mode: "{{ result.response.discharge_mode }}"
      charger_mode: "{{ result.response.charger_mode }}"
  # Example: Set inverter mode based on recommendation
  - service: select.select_option
    data:
      option: "{{ discharge_mode }}"
    target:
      entity_id: select.inverter_discharge_mode
  - service: select.select_option
    data:
      option: "{{ charger_mode }}"
    target:
      entity_id: select.inverter_charger_mode
```

### Output Sensors

The integration creates the following sensor entities for monitoring:

| Entity | Description | Values |
|---|---|---|
| `sensor.battery_optimizer_discharge_mode` | Recommended discharge mode | `SUB`, `SBU` |
| `sensor.battery_optimizer_charger_mode` | Recommended charger mode | `CSO`, `SNU`, `OSO` |
| `sensor.battery_optimizer_recommended_soc` | Expected final SoC after horizon | Percentage (e.g., "62%") |

## Testing

### Run All Tests

```bash
cd custom_components/cvxpy_optimizer
pytest tests/ -v
```

### Run with Coverage

```bash
pip install pytest-cov
pytest tests/ -v --cov=. --cov-report=term-missing
```

### Test Categories

| File | Description |
|---|---|
| `tests/test_fetcher.py` | Unit tests for data fetching and SoC estimation |
| `tests/test_optimizer.py` | Unit + scenario-based tests (A/B/C/D) |
| `tests/test_decider.py` | Mode decision logic tests |
| `tests/test_integration.py` | E2E integration tests with mocked HA + randomized pricing |

### Scenarios Covered

- **Scenario A:** Negative prices → SNU mode for arbitrage
- **Scenario B:** Expensive day → battery discharges during peaks
- **Scenario C:** Low SoC → limits discharge, prioritizes charging
- **Scenario D:** Mixed prices → balanced charge/discharge schedule
- **Randomized pricing:** Volatile, mostly-negative, flat, spike-heavy profiles

## Project Structure

```
custom_components/cvxpy_optimizer/
├── __init__.py              # Integration entry point + service handler
├── manifest.json            # HA integration metadata + dependencies
├── config_flow.py           # Configuration flow (extensible)
├── const.py                 # Constants and battery parameters
├── fetcher.py               # Data retrieval from HA sensors
├── optimizer.py             # CVXPY LP optimization engine
├── decider.py               # Mode decision logic (SUB/SBU/CSO/SNU/OSO)
├── sensor.py                # Output sensor entities
├── services.yaml            # Service definitions
├── pyproject.toml           # Build config + tool settings
├── requirements.txt         # Python dependencies
└── tests/                   # PyTest test suite
    ├── __init__.py
    ├── conftest.py          # Shared fixtures
    ├── test_fetcher.py
    ├── test_optimizer.py
    ├── test_decider.py
    └── test_integration.py
```

## How It Works

### 1. Data Fetching (`fetcher.py`)

The fetcher retrieves real-time data from Home Assistant:
- **Prices:** Hourly electricity prices from Pstryk AIO integration
- **Battery State:** Voltage → SoC conversion using LiFePO4 discharge curve
- **Load:** Current household power consumption

### 2. Optimization (`optimizer.py`)

A CVXPY linear program minimizes total cost:

**Decision Variables:**
- `charge_power[t]` – Power into battery (W) at time step t
- `discharge_power[t]` – Power from battery (W) at time step t
- `soc[t]` – State of Charge at time step t
- `grid_power[t]` – Net power drawn from grid

**Constraints:**
- Power limits: 0 ≤ charge/discharge ≤ max power (~1680W)
- SoC bounds: 5% ≤ SoC ≤ 100%
- Energy balance: Grid = Load − Discharge + Charge (with efficiency losses)
- SoC dynamics: Battery level changes based on charge/discharge

**Objective:** Minimize Σ(price[t] × grid_power[t])

### 3. Decision Logic (`decider.py`)

Translates numerical results into HA-compatible modes:
- **Negative prices → SNU** (arbitrage)
- **High discharge activity + sufficient SoC → SBU**
- **Low SoC (<20%) → SUB** (conserve battery)
- **4+ cheap hours → CSO** (charge from grid during cheap periods)
- **Near-full (>90%) → OSU** (solar only)

## License

MIT License
