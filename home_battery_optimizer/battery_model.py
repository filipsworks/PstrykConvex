"""Battery model: constants, SOC estimation from voltage."""

# Pack specs (8s LiFePo4)
NOMINAL_VOLTAGE = 26.0  # V — nominal pack voltage
MAX_CHARGE_CURRENT_A = 70  # A — absolute max charge current
CAPACITY_AH = 320  # Ah — cell capacity

# SOC bounds (DoD limits to prolong life)
MIN_SOC = 0.10  # 10% depth of discharge
MAX_SOC = 0.95  # 95% top of charge

# Voltage range for linear SOC mapping
VOLTAGE_MIN = 24.0  # V → 0% SOC
VOLTAGE_MAX = 28.0  # V → 100% SOC

# Round-trip efficiency breakdown
CHARGE_EFFICIENCY = 0.95  # BMS + wiring losses during charge
DISCHARGE_EFFICIENCY = 0.95  # inverter losses during discharge
ROUND_TRIP_EFFICIENCY = CHARGE_EFFICIENCY * DISCHARGE_EFFICIENCY

# Derived
TOTAL_CAPACITY_WH = NOMINAL_VOLTAGE * CAPACITY_AH  # ~8320 Wh (V × Ah = Wh)
USABLE_CAPACITY_WH = TOTAL_CAPACITY_WH * (MAX_SOC - MIN_SOC)


def voltage_to_soc(voltage: float) -> float:
    """Estimate SOC from pack voltage using linear mapping."""
    soc = (voltage - VOLTAGE_MIN) / (VOLTAGE_MAX - VOLTAGE_MIN)
    return max(0.0, min(1.0, soc))


def current_to_kw(current_a: float, voltage: float) -> float:
    """Convert A × V to kW."""
    return abs(current_a * voltage) / 1000.0


def max_charge_power_kw() -> float:
    """Maximum practical charge power in kW at nominal voltage."""
    return MAX_CHARGE_CURRENT_A * NOMINAL_VOLTAGE / 1000
