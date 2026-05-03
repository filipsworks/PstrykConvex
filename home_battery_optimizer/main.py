#!/usr/bin/env python3
"""Home battery charging optimizer — entry point with TUI."""

import argparse
import sys

from api import fetch_all_data
from battery_model import TOTAL_CAPACITY_WH, voltage_to_soc
from optimizer import optimize

# Realistic sample prices based on Pstryk pricing patterns (PLN/kWh)
MOCK_PRICES = [
    {"hour": 0, "price": 0.813, "is_cheap": False, "is_expensive": False},
    {"hour": 1, "price": 0.809, "is_cheap": False, "is_expensive": False},
    {"hour": 2, "price": 0.802, "is_cheap": False, "is_expensive": False},
    {"hour": 3, "price": 0.797, "is_cheap": False, "is_expensive": False},
    {"hour": 4, "price": 0.815, "is_cheap": False, "is_expensive": False},
    {"hour": 5, "price": 0.856, "is_cheap": False, "is_expensive": False},
    {"hour": 6, "price": 1.333, "is_cheap": False, "is_expensive": True},
    {"hour": 7, "price": 1.321, "is_cheap": False, "is_expensive": True},
    {"hour": 8, "price": 1.236, "is_cheap": False, "is_expensive": False},
    {"hour": 9, "price": 0.980, "is_cheap": False, "is_expensive": False},
    {"hour": 10, "price": 0.682, "is_cheap": True, "is_expensive": False},
    {"hour": 11, "price": 0.664, "is_cheap": True, "is_expensive": False},
    {"hour": 12, "price": 0.641, "is_cheap": True, "is_expensive": False},
    {"hour": 13, "price": 0.658, "is_cheap": True, "is_expensive": False},
    {"hour": 14, "price": 0.674, "is_cheap": True, "is_expensive": False},
    {"hour": 15, "price": 0.261, "is_cheap": True, "is_expensive": False},
    {"hour": 16, "price": 0.404, "is_cheap": True, "is_expensive": False},
    {"hour": 17, "price": 1.135, "is_cheap": False, "is_expensive": False},
    {"hour": 18, "price": 1.284, "is_cheap": False, "is_expensive": False},
    {"hour": 19, "price": 1.411, "is_cheap": False, "is_expensive": True},
    {"hour": 20, "price": 1.568, "is_cheap": False, "is_expensive": True},
    {"hour": 21, "price": 1.386, "is_cheap": False, "is_expensive": True},
    {"hour": 22, "price": 0.892, "is_cheap": False, "is_expensive": False},
    {"hour": 23, "price": 0.839, "is_cheap": False, "is_expensive": False},
]

# Realistic dummy load profile (kW) — typical home baseline
MOCK_DUMMY_LOADS = [
    0.80,
    0.75,
    0.72,
    0.70,
    0.72,
    0.75,  # 00-05: night baseline
    0.90,
    1.00,
    1.20,
    1.30,
    1.50,
    1.60,  # 06-11: morning ramp
    1.50,
    1.40,
    1.50,
    1.60,
    1.80,
    2.00,  # 12-17: afternoon peak
    2.20,
    2.50,
    2.00,
    1.50,
    1.20,
    1.00,  # 18-23: evening + wind-down
]


def render_tui(decisions: list[dict], summary: dict) -> str:
    """Render the TUI bar chart and summary as a string."""
    lines = []
    sep = "─" * 72

    lines.append(
        f"  🪫 Home Battery Optimization — {summary['total_charge_wh']:.0f} Wh charged / {summary['total_discharge_wh']:.0f} Wh discharged"
    )
    lines.append(sep)
    lines.append("")

    # Find max power for scaling (charge or discharge, whichever is larger)
    max_power = 0.01  # avoid div by zero
    for d in decisions:
        val = max(d["charge_wh"], d["discharge_wh"])
        if val > max_power:
            max_power = val

    bar_width = 40

    lines.append(
        f"  {'Hr':>3} | {'Mode':<8} | Price   | Charge      | Dischg      | SOC%   | Grid Cost"
    )
    lines.append(
        f"  {'─' * 3}┼{'─' * 8}┼{'─' * 8}┼{'─' * 14}┼{'─' * 14}┼{'─' * 7}┼{'─' * 10}"
    )

    for d in decisions:
        hour = d["hour"]
        mode = f"{d['output_mode']}/{d['charger_mode']}"
        price = f"{d['price_plkwh']:.3f}"

        # Charge bar (green)
        if d["charge_wh"] > 0:
            ch_len = max(1, int(d["charge_wh"] / max_power * bar_width))
            charge_bar = "█" * ch_len
        else:
            charge_bar = "." * bar_width

        # Discharge bar (blue)
        if d["discharge_wh"] > 0:
            dis_len = max(1, int(d["discharge_wh"] / max_power * bar_width))
            discharge_bar = "█" * dis_len
        else:
            discharge_bar = "." * bar_width

        soc = f"{d['soc_pct']:5.1f}"
        cost = f"{d['grid_cost_pln']:.4f}"

        # Color coding via ANSI — but keep it simple with mode indicators
        color_map = {
            "SUB/OSO": "\033[90m",  # gray (idle)
            "SBU/OSO": "\033[94m",  # blue (discharging)
            "SUB/SNU": "\033[92m",  # green (charging)
            "SBU/SNU": "\033[93m",  # yellow (special case E)
        }
        reset = "\033[0m"
        color = color_map.get(mode, "\033[90m")

        line = (
            f"  {hour:3d}│{color}{mode:<8}{reset}│{price:>7} │"
            f"{charge_bar} {d['charge_wh'] / 1000:5.2f}kWh│"
            f"{discharge_bar} {d['discharge_wh'] / 1000:5.2f}kWh│"
            f"{soc}%  │{cost}"
        )
        lines.append(line)

    lines.append("")
    lines.append(sep)
    lines.append("  📊 Daily Summary")
    lines.append(sep)
    lines.append(f"  Total charged:      {summary['total_charge_wh']:>8.1f} Wh")
    lines.append(f"  Total discharged:   {summary['total_discharge_wh']:>8.1f} Wh")
    lines.append(f"  Net energy (in-out):{summary['net_energy_wh']:>+8.1f} Wh")
    lines.append(f"  Cycled:             {summary['cycled_pct']:>8.2f}% of capacity")
    lines.append(f"  Grid used:          {summary['total_grid_kwh']:>8.3f} kWh")
    lines.append(f"  Total cost:         {summary['total_cost_pln']:>8.4f} PLN")
    lines.append(sep)

    # Legend
    lines.append("")
    lines.append("  Legend:")
    lines.append("    SUB/OSO — loads from grid, no charging (default)")
    lines.append("    SBU/OSO — loads from battery, no charging")
    lines.append("    SUB/SNU — loads from grid + charge from utility")
    lines.append(
        "    SUB/OSO*— special case E: discharge to loads while grid supplies dummy load"
    )
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Home battery charging optimizer")
    parser.add_argument(
        "--mock", action="store_true", help="Run with sample data (no API required)"
    )
    args = parser.parse_args()

    if args.mock:
        print("Running in MOCK mode with sample data...")
        prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
        dummy_loads = MOCK_DUMMY_LOADS
        initial_soc = 0.35  # ~25V → 37.5% SOC (typical morning level)
    else:
        print("Fetching data from Home Assistant...")
        try:
            data = fetch_all_data()
        except Exception as e:
            print(f"Error fetching data: {e}", file=sys.stderr)
            print("Falling back to MOCK mode...", file=sys.stderr)
            prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
            dummy_loads = MOCK_DUMMY_LOADS
            initial_soc = 0.35
        else:
            prices = data["prices"]
            dummy_loads = data["dummy_loads"]
            voltage = data["voltage"]

            # Determine initial SOC
            if voltage is not None:
                initial_soc = voltage_to_soc(voltage)
                print(
                    f"  Battery voltage: {voltage:.1f} V → SOC: {initial_soc * 100:.1f}%"
                )
            else:
                initial_soc = 0.5
                print("  No voltage reading — using default SOC: 50%")

    # Print price overview
    cheap_hours = [p["hour"] for p in prices if p.get("is_cheap")]
    expensive_hours = [p["hour"] for p in prices if p.get("is_expensive")]
    print(f"  Cheap hours:   {sorted(cheap_hours)}")
    print(f"  Expensive hrs: {sorted(expensive_hours)}")

    # Run optimization
    print("\nRunning CVXPY optimizer...")
    try:
        result = optimize(prices, dummy_loads, initial_soc)
    except Exception as e:
        print(f"Optimization failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Render TUI
    output = render_tui(result["decisions"], result["summary"])
    print(output)


if __name__ == "__main__":
    main()
