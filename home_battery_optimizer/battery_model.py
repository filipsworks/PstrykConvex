"""Battery model: constants, SOC estimation from voltage."""

# Pack specs (16s LiFePo4 — 2026-09 upgrade from 8s/24 V, cell count doubled
# in series so pack voltage doubled while Ah stayed the same → 2× energy)
NOMINAL_VOLTAGE = 52.0  # V — nominal pack voltage
MAX_CHARGE_CURRENT_A = 90  # A — absolute max charge current
CAPACITY_AH = 320  # Ah — cell capacity

# SOC bounds (DoD limits to prolong life)
MIN_SOC = 0.10  # 10% depth of discharge
MAX_SOC = 0.95  # 95% top of charge

# Voltage range for linear SOC mapping (16s LiFePo4 pack)
# 48.0 V = 0% SOC (cutoff), 56.0 V = 100% SOC (full charge) — 3.0/3.5 V per cell,
# matching the inverter's own maximum_charging_voltage (56.0 V) and OP1 off-grid
# low-voltage protection (48.0 V).
# Voltages above 56.0 V can occur during active charging — voltage_to_soc clamps to 100%.
VOLTAGE_MIN = 48.0  # V → 0% SOC
VOLTAGE_MAX = 56.0  # V → 100% SOC

# Round-trip efficiency breakdown
CHARGE_EFFICIENCY = 0.95  # BMS + wiring losses during charge
DISCHARGE_EFFICIENCY = 0.95  # inverter losses during discharge
ROUND_TRIP_EFFICIENCY = CHARGE_EFFICIENCY * DISCHARGE_EFFICIENCY

# Discrete charging steps: 10A-90A in 10A increments (minimum practical charge).
# Written to number.…_maximum_mains_charging_current (accepts 5–120 A).
CHARGE_STEPS_A = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]

# Ceiling on grid→battery power used by the optimizer. Non-binding at the
# current 90 A / 52 V (4.68 kW) — lower it to throttle grid charging.
MAX_CHARGE_POWER_KW = 5.0

# Derived
TOTAL_CAPACITY_WH = NOMINAL_VOLTAGE * CAPACITY_AH  # ~16640 Wh (V × Ah = Wh)
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


if __name__ == "__main__":
    # Self-check: the 8s→16s transposition keeps per-cell voltages and Ah,
    # so pack energy must exactly double and the SOC map must stay anchored.
    CELLS = 16
    assert VOLTAGE_MIN / CELLS == 3.0, VOLTAGE_MIN / CELLS
    assert VOLTAGE_MAX / CELLS == 3.5, VOLTAGE_MAX / CELLS
    assert NOMINAL_VOLTAGE / CELLS == 3.25, NOMINAL_VOLTAGE / CELLS
    assert TOTAL_CAPACITY_WH == 16640.0, TOTAL_CAPACITY_WH
    assert voltage_to_soc(VOLTAGE_MIN) == 0.0
    assert voltage_to_soc(VOLTAGE_MAX) == 1.0
    assert voltage_to_soc(40.0) == 0.0 and voltage_to_soc(60.0) == 1.0  # clamped
    assert max(CHARGE_STEPS_A) == MAX_CHARGE_CURRENT_A
    assert max_charge_power_kw() <= MAX_CHARGE_POWER_KW  # ceiling stays non-binding
    print("battery_model self-check OK")
