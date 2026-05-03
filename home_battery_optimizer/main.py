#!/usr/bin/env python3
"""Home battery charging optimizer — CLI entry point with TUI / JSON output."""

import argparse
import json
import sys
from datetime import datetime, timezone

from api import fetch_all_data
from battery_model import TOTAL_CAPACITY_WH, voltage_to_soc
from optimizer import optimize

# ── Mock data (used when --mock or API unavailable) ────────────────────────

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


# ── CLI parsing ────────────────────────────────────────────────────────────


def parse_args():
    p = argparse.ArgumentParser(
        description="Home battery charging optimizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Mock run (no API):
  %(prog)s --mock

  # Live data, today's prices, TUI output:
  %(prog)s --ha-url https://ha.example.com --ha-token YOUR_TOKEN

  # JSON output for tomorrow, last 3 days of history:
  %(prog)s --ha-url ... --ha-token ... --horizon tomorrow --days 3 --output json

Mode legend:
  SUB/SNU — loads on grid + charge battery from grid (cheap hours)
  SBU/OSO — loads on battery, no charging (default)
  SUB/OSO — loads on grid, no charging (idle battery)
""",
    )
    p.add_argument("--mock", action="store_true", help="Use sample data instead of API")
    p.add_argument(
        "--ha-url",
        default="",
        help="Home Assistant base URL (e.g. https://ha.example.com)",
    )
    p.add_argument("--ha-token", default="", help="Long-lived access token")
    p.add_argument(
        "--horizon",
        choices=["today", "tomorrow", "available"],
        default="available",
        help="Which day's prices to optimize for (default: available)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days to fetch history for (default: 1)",
    )
    p.add_argument(
        "--output",
        choices=["tui", "json"],
        default="tui",
        help="Output format (default: tui)",
    )
    return p.parse_args()


# ── Data fetching ──────────────────────────────────────────────────────────


def get_data(args):
    """Return (prices, dummy_loads, initial_soc) for the requested horizon."""
    if args.mock or (not args.ha_url and not args.ha_token):
        print("[mock] Using sample data", file=sys.stderr)
        prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
        dummy_loads = MOCK_DUMMY_LOADS
        initial_soc = 0.35
    else:
        # Override API client with CLI args
        import api as api_mod

        api_mod.HA_BASE_URL = args.ha_url.rstrip("/")
        api_mod.HA_TOKEN = args.ha_token

        print(f"Fetching data from {args.ha_url} ...", file=sys.stderr)
        try:
            data = api_mod.fetch_all_data(days=args.days, horizon=args.horizon)
        except Exception as e:
            print(f"[error] API failed ({e}), falling back to mock", file=sys.stderr)
            prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
            dummy_loads = MOCK_DUMMY_LOADS
            initial_soc = 0.35
        else:
            prices = data["prices"]
            dummy_loads = data["dummy_loads"]
            voltage = data.get("voltage")
            if voltage is not None:
                initial_soc = voltage_to_soc(voltage)
                print(
                    f"  Voltage {voltage:.1f} V → SOC {initial_soc * 100:.1f}%",
                    file=sys.stderr,
                )
            else:
                initial_soc = 0.5
                print("  No voltage reading — defaulting to SOC 50%", file=sys.stderr)

    return prices, dummy_loads, initial_soc


# ── TUI rendering ──────────────────────────────────────────────────────────


def render_tui(all_results: list[dict]) -> str:
    """Render multi-day results as aligned ASCII table with colored bars."""
    lines = []
    sep = "─" * 80

    for day_idx, result in enumerate(all_results):
        decisions = result["decisions"]
        summary = result["summary"]
        date_label = f"Day {day_idx + 1}" if len(all_results) > 1 else "Today"

        lines.append(f"\n  📅 {date_label}")
        lines.append(sep)

        # Column widths (computed from data for perfect alignment)
        mode_w = max(
            8, max(len(d["output_mode"] + "/" + d["charger_mode"]) for d in decisions)
        )
        price_w = 7
        charge_w = 10  # e.g. "+1.82 kWh" or "-1.44 kWh"
        soc_w = 7  # " 35.0%"
        cost_w = 9  # "   0.6504"

        header_fmt = (
            f"  {{hr:>3}} │ {{mode:<{mode_w}}} │ {{price:>{price_w}s}} │ "
            f"{{charge:>{charge_w}s}} │ {{soc:>{soc_w}s}} │ {{cost:>{cost_w}s}}"
        )
        sep_fmt = (
            "  "
            + "─" * 3
            + "┼"
            + "─" * mode_w
            + "┼"
            + "─" * (price_w + 2)
            + "┼"
            + "─" * charge_w
            + "┼"
            + "─" * soc_w
            + "┼"
            + "─" * cost_w
        )

        lines.append(
            header_fmt.format(
                hr="Hr",
                mode="Mode",
                price="Price",
                charge="Charge",
                soc="SOC%",
                cost="Cost",
            )
        )
        lines.append(sep_fmt)

        # Find max absolute charge for bar scaling per day
        max_abs = 0.01
        for d in decisions:
            val = abs(d["charge_kwh"])
            if val > max_abs:
                max_abs = val

        bar_width = 20

        for d in decisions:
            hour = d["hour"]
            mode_str = f"{d['output_mode']}/{d['charger_mode']}"
            price_str = f"{d['price_plkwh']:.3f}"

            # Charge/discharge value with sign
            ch_kwh = d["charge_kwh"]  # positive = charge, negative = discharge
            if abs(ch_kwh) < 0.005:
                charge_str = "    idle"
                bar_str = "." * bar_width
                color = "\033[90m"  # gray
            elif ch_kwh > 0:
                charge_str = f"+{ch_kwh:.2f} kWh"
                blen = max(1, int(ch_kwh / max_abs * bar_width))
                bar_str = "█" * blen + "." * (bar_width - blen)
                color = "\033[92m"  # green
            else:
                charge_str = f"{ch_kwh:.2f} kWh"
                blen = max(1, int(abs(ch_kwh) / max_abs * bar_width))
                bar_str = "█" * blen + "." * (bar_width - blen)
                color = "\033[94m"  # blue

            reset = "\033[0m"
            soc_str = f"{d['soc_pct']:5.1f}%"
            cost_str = f"{d['grid_cost_pln']:.4f}"

            line = (
                f"  {hour:3d}│{color}{mode_str:<{mode_w}}{reset}│"
                f" {price_str:>6} │"
                f"{color}{bar_str}{reset} {charge_str:>10s} │"
                f" {soc_str:>5s} │"
                f" {cost_str:>{cost_w}}"
            )
            lines.append(line)

        # Day summary
        lines.append(sep)
        lines.append(f"  📊 Summary — {date_label}")
        lines.append(sep)
        lines.append(f"    Charged:       {summary['total_charge_wh']:>8.0f} Wh")
        lines.append(f"    Discharged:    {summary['total_discharge_wh']:>8.0f} Wh")
        lines.append(f"    Net (in-out):  {summary['net_energy_wh']:>+8.0f} Wh")
        lines.append(f"    Cycled:        {summary['cycled_pct']:>8.2f}% of capacity")
        lines.append(f"    Grid used:     {summary['total_grid_kwh']:>8.3f} kWh")
        lines.append(f"    Total cost:    {summary['total_cost_pln']:>8.4f} PLN")

    # Legend (once at end)
    if all_results:
        lines.append("")
        lines.append(sep)
        lines.append("  Mode legend:")
        lines.append(
            "    SUB/SNU — loads on grid + charge battery from grid   [green bar]"
        )
        lines.append(
            "    SBU/OSO — loads on battery, no charging              [blue bar]"
        )
        lines.append(
            "    SUB/OSO — loads on grid, no charging (idle battery)  [gray bar]"
        )
        lines.append("")

    return "\n".join(lines)


# ── JSON rendering ─────────────────────────────────────────────────────────


def render_json(all_results: list[dict]) -> str:
    """Return results as pretty-printed JSON."""
    return json.dumps(all_results, indent=2)


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    args = parse_args()

    # Fetch data
    prices, dummy_loads, initial_soc = get_data(args)

    # Run optimization for each day in the horizon
    all_results = []
    n_days = (
        1
        if args.horizon == "today"
        else (2 if args.horizon == "tomorrow" else len(prices) // 24 + 1)
    )
    # Actually, prices is always 24h. For multi-day we'd need more data.
    # For now, optimize one day at a time from the price list.
    for i in range(min(n_days, len(prices) // 24)):
        start = i * 24
        end = min(start + 24, len(prices))
        if start >= end:
            break
        day_prices = prices[start:end]
        # Pad or slice dummy loads to match
        day_loads = (dummy_loads * ((end - start) // len(dummy_loads) + 1))[start:end]

        soc_start = (
            initial_soc
            if i == 0
            else all_results[-1]["summary"]["final_soc"]
            if all_results
            else initial_soc
        )

        try:
            result = optimize(day_prices, day_loads, soc_start)
        except Exception as e:
            print(f"[error] Optimization failed for day {i + 1}: {e}", file=sys.stderr)
            sys.exit(1)

        # Inject charge_kwh (signed) into each decision
        for d in result["decisions"]:
            d["charge_kwh"] = round(d["charge_wh"] / 1000 - d["discharge_wh"] / 1000, 4)

        all_results.append(result)

    # Output
    if args.output == "json":
        print(render_json(all_results))
    else:
        print(render_tui(all_results))


if __name__ == "__main__":
    main()
