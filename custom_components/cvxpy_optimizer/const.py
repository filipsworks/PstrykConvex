"""Constants for the CVXPY Battery Optimizer integration."""

from __future__ import annotations

# ── Domain ────────────────────────────────────────────────────────────────
DOMAIN: str = "cvxpy_optimizer"
SERVICE_OPTIMIZE_BATTERY: str = "optimize_battery"

# ── Sensor Entity IDs (must match real HA entities) ──────────────────────
SENSOR_TODAY_PRICES: str = "sensor.pstryk_aio_obecna_cena_zakupu_pradu"
SENSOR_TOMORROW_PRICES: str = "sensor.pstryk_aio_cena_zakupu_pradu_jutro"
SENSOR_LOAD_POWER: str = "sensor.gniazdo_output_active_power"
SENSOR_BATTERY_VOLTAGE: str = "sensor.gniazdo_battery_voltage"
SENSOR_BATTERY_CURRENT: str = "sensor.gniazdo_battery_discharge_current"

# ── Output Sensor Entity IDs ─────────────────────────────────────────────
OUTPUT_SENSOR_DISCHARGE_MODE: str = "battery_optimizer_discharge_mode"
OUTPUT_SENSOR_CHARGER_MODE: str = "battery_optimizer_charger_mode"
OUTPUT_SENSOR_RECOMMENDED_SOC: str = "battery_optimizer_recommended_soc"

# ── Discharge Modes (Battery Usage) ──────────────────────────────────────
DISCHARGE_SUB: str = "SUB"   # Solar First – Utility + Solar priority, battery as backup
DISCHARGE_SBU: str = "SBU"   # Solar + Battery First – Battery and solar prioritized over grid

# ── Charger Modes (Battery Charging) ─────────────────────────────────────
CHARGER_CSO: str = "CSO"    # Charge Solar + Utility
CHARGER_SNU: str = "SNU"    # Solar + Negative Utility (forced arbitrage when price < 0)
CHARGER_OSU: str = "OSO"    # Solar Only

# ── Battery Configuration (8S LiFePO4) ───────────────────────────────────
BATTERY_CHEMISTRY: str = "LiFePO4"
BATTERY_CELL_VOLTAGE: float = 3.2          # Volts per cell (nominal)
BATTERY_SERIES_COUNT: int = 8              # 8S configuration
BATTERY_NOMINAL_VOLTAGE: float = 24.0      # 8 × 3.0V nominal pack voltage
BATTERY_MIN_VOLTAGE: float = 24.0          # Minimum operating voltage (V)
BATTERY_MAX_VOLTAGE: float = 28.0          # Maximum operating voltage (V)
BATTERY_CAPACITY_AMP_HOURS: float = 320.0  # Capacity in Ah
BATTERY_ENERGY_WH: float = (
    BATTERY_NOMINAL_VOLTAGE * BATTERY_CAPACITY_AMP_HOURS  # 9600 Wh
)
MAX_CHARGE_CURRENT: float = 70.0   # Maximum charging current (A)
MAX_DISCHARGE_CURRENT: float = 70.0 # Maximum discharging current (A)
MAX_CHARGE_POWER: float = BATTERY_NOMINAL_VOLTAGE * MAX_CHARGE_CURRENT       # 1680 W
MAX_DISCHARGE_POWER: float = BATTERY_NOMINAL_VOLTAGE * MAX_DISCHARGE_CURRENT # 1680 W

# ── SoC Bounds (derived from voltage range for LiFePO4) ──────────────────
# LiFePO4 voltage-SOC curve approximation:
#   ~24.0V → 5% SOC  (practical minimum to protect cells)
#   ~25.6V → 20% SOC
#   ~27.2V → 80% SOC
#   ~28.0V → 100% SOC (fully charged)
SOC_MIN: float = 0.05    # 5% minimum SoC (protects cells from deep discharge)
SOC_MAX: float = 1.0     # 100% maximum SoC

# ── Optimization Horizon ─────────────────────────────────────────────────
HORIZON_HOURS: int = 24   # Default optimization horizon in hours
TIME_STEP_MINUTES: int = 60  # Time step for optimization (1 hour)

# ── Efficiency ────────────────────────────────────────────────────────────
BATTERY_ROUND_TRIP_EFFICIENCY: float = 0.95  # Overall round-trip efficiency
CHARGE_EFFICIENCY: float = 0.97               # Charging efficiency factor
DISCHARGE_EFFICIENCY: float = 0.97            # Discharging efficiency factor

# ── Price Data Attribute Keys ────────────────────────────────────────────
ATTR_TODAY_PRICES: str = "today_prices"
ATTR_TOMORROW_PRICES: str = "tomorrow_prices"
ATTR_PRICE: str = "price"
ATTR_START: str = "start"
ATTR_END: str = "end"

# ── Optimization Result Keys ─────────────────────────────────────────────
RESULT_DISCHARGE_MODE: str = "discharge_mode"
RESULT_CHARGER_MODE: str = "charger_mode"
RESULT_SCHEDULE: str = "schedule"
RESULT_COST: str = "estimated_cost"
RESULT_SOC_TRAJECTORY: str = "soc_trajectory"

# ── Schedule Entry Keys ──────────────────────────────────────────────────
SCHED_TIME: str = "time"
SCHED_CHARGE_POWER: str = "charge_power_w"
SCHED_DISCHARGE_POWER: str = "discharge_power_w"
SCHED_SOC: str = "soc"
SCHED_GRID_POWER: str = "grid_power_w"
