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
        default="https://ha-finland.kompfix.pl",
        help="Home Assistant base URL (e.g. https://ha-finland.kompfix.pl). /api is appended automatically.",
    )
    p.add_argument(
        "--ha-token",
        default="",
        help="Long-lived access token (required unless --mock)",
    )
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
        choices=["tui", "json", "sensitivity", "sensitivity-json"],
        default="tui",
        help="Output format (default: tui). 'sensitivity'/'sensitivity-json' shows grid cost vs target SOC.",
    )
    p.add_argument(
        "--target-soc",
        type=float,
        default=None,
        metavar="PCT",
        help="Target end-of-day SOC in percent (0–100). Default: no constraint.",
    )
    return p.parse_args()


# ── Data fetching ──────────────────────────────────────────────────────────


def _build_base_url(ha_url: str) -> str:
    """Ensure base URL ends with /api."""
    url = ha_url.rstrip("/")
    if not url.endswith("/api"):
        url += "/api"
    return url


def get_data(args):
    """Return (prices, dummy_loads, initial_soc) for the requested horizon."""
    if args.mock:
        print("[mock] Using sample data", file=sys.stderr)
        prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
        dummy_loads = MOCK_DUMMY_LOADS
        initial_soc = 0.35
    else:
        base_url = _build_base_url(args.ha_url)

        print(f"Fetching data from {base_url} ...", file=sys.stderr)
        try:
            import api as api_mod

            data = api_mod.fetch_all_data(
                base_url=base_url,
                token=args.ha_token,
                days=args.days,
                horizon=args.horizon,
            )
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


def render_tui(all_results: list[dict], horizon: str = "available") -> str:
    """Render multi-day results as aligned ASCII table with colored bars."""
    lines = []
    sep = "─" * 80

    for day_idx, result in enumerate(all_results):
        decisions = result["decisions"]
        summary = result["summary"]
        # Use horizon-based label for single-result runs; otherwise Day N
        if len(all_results) == 1 and horizon != "available":
            date_label = horizon.capitalize()
        else:
            date_label = f"Day {day_idx + 1}"

        lines.append(f"\n  📅 {date_label}")
        lines.append(sep)

        # Column widths (computed from data for perfect alignment)
        mode_w = max(
            8, max(len(d["output_mode"] + "/" + d["charger_mode"]) for d in decisions)
        )
        price_w = 7
        charge_w = 14  # e.g. "+1.82 kWh @ 70A" or "-1.44 kWh"
        soc_w = 7  # " 35.0%"
        grid_cost_w = 9  # actual grid spend: "    0.6504"
        total_cost_w = 9  # total active power cost: "    1.2840"

        header_fmt = (
            f"  {{hr:>3}} │ {{mode:<{mode_w}}} │ {{price:>{price_w}s}} │ "
            f"{{charge:>{charge_w}s}} │ {{soc:>{soc_w}s}} │ "
            f"{{grid_cost:>{grid_cost_w}s}} │ {{total_cost:>{total_cost_w}s}}"
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
            + "─" * grid_cost_w
            + "┼"
            + "─" * total_cost_w
        )

        lines.append(
            header_fmt.format(
                hr="Hr",
                mode="Mode",
                price="Price",
                charge="Charge",
                soc="SOC%",
                grid_cost="Grid Cost",
                total_cost="Total Cost",
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

            # Charge/discharge value with sign and amps (for charging)
            ch_kwh = d["charge_kwh"]  # positive = charge, negative = discharge
            charge_amps = d.get("charge_amps", 0)
            if abs(ch_kwh) < 0.005:
                charge_str = "    idle"
                bar_str = "." * bar_width
                color = "\033[90m"  # gray
            elif ch_kwh > 0:
                charge_str = f"+{ch_kwh:.2f} kWh @ {charge_amps}A"
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
            grid_cost_str = f"{d['grid_cost_pln']:.4f}"
            total_cost_str = f"{d['total_cost_pln']:.4f}"

            line = (
                f"  {hour:3d}│{color}{mode_str:<{mode_w}}{reset}│"
                f" {price_str:>6} │"
                f"{color}{bar_str}{reset} {charge_str:>14s} │"
                f" {soc_str:>5s} │"
                f" {grid_cost_str:>{grid_cost_w}} │"
                f" {total_cost_str:>{total_cost_w}}"
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

    # Validate: either --mock or both --ha-url and --ha-token required
    if not args.mock and not args.ha_token:
        print(
            "[error] Either --mock or --ha-token is required. Use --help for usage.",
            file=sys.stderr,
        )
        sys.exit(1)

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

        # Convert target SOC from percent to fraction (0–1)
        target_soc = args.target_soc / 100 if args.target_soc is not None else None

        try:
            result = optimize(day_prices, day_loads, soc_start, target_soc=target_soc)
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
    elif args.output == "sensitivity":
        print(render_sensitivity(prices, dummy_loads, initial_soc))
    elif args.output == "sensitivity-json":
        render_sensitivity_json(prices, dummy_loads, initial_soc)
    else:
        print(render_tui(all_results, horizon=args.horizon))


# ── Sensitivity analysis ───────────────────────────────────────────────────


def run_sensitivity(
    prices: list[dict], dummy_loads_kw: list[float], initial_soc: float
) -> dict:
    """Run unconstrained + sensitivity optimization and return results.

    Returns dict with:
      - baseline: result from unconstrained optimization (no target SOC)
      - points: list of dicts for each target SOC level (0-100%, step 5%)
        with deltas vs baseline
    """
    # First, run unconstrained to find the "natural" optimal SOC
    print("Running unconstrained optimization (baseline)...", file=sys.stderr)
    try:
        baseline_result = optimize(prices, dummy_loads_kw, initial_soc, target_soc=None)
        baseline_cost_pln = baseline_result["summary"]["total_cost_pln"]
        baseline_grid_kwh = baseline_result["summary"]["total_grid_kwh"]
        baseline_cost_per_kwh = (
            baseline_cost_pln / max(baseline_grid_kwh, 0.001)
            if baseline_grid_kwh > 0
            else 0
        )
        # Find what SOC the optimizer naturally chose
        last_soc_pct = baseline_result["summary"]["final_soc"]
    except Exception as e:
        print(f"  [warn] Baseline optimization failed: {e}", file=sys.stderr)
        baseline_cost_pln = 0
        baseline_grid_kwh = 1
        baseline_cost_per_kwh = 0
        last_soc_pct = None

    # Now run sensitivity for each target SOC level
    points = []
    soc_levels = range(0, 105, 5)  # 0%, 5%, ..., 100%

    print(
        f"Running sensitivity analysis ({len(soc_levels)} scenarios)...",
        file=sys.stderr,
    )

    for target_pct in soc_levels:
        target_soc = target_pct / 100.0
        try:
            result = optimize(
                prices, dummy_loads_kw, initial_soc, target_soc=target_soc
            )
            summary = result["summary"]
            total_cost_pln = summary["total_cost_pln"]
            grid_kwh = summary["total_grid_kwh"]
            cost_per_kwh = total_cost_pln / max(grid_kwh, 0.001) if grid_kwh > 0 else 0

            # Delta vs baseline
            delta_cost_pln = total_cost_pln - baseline_cost_pln
            delta_cost_per_kwh = cost_per_kwh - baseline_cost_per_kwh

            points.append(
                {
                    "target_soc": target_pct,
                    "total_cost_pln": round(total_cost_pln, 4),
                    "grid_kwh": round(grid_kwh, 3),
                    "cost_per_kwh": round(cost_per_kwh, 6),
                    "delta_cost_pln": round(delta_cost_pln, 4),
                    "delta_cost_per_kwh": round(delta_cost_per_kwh, 6),
                }
            )
        except Exception as e:
            print(f"  [warn] Target SOC {target_pct}% failed: {e}", file=sys.stderr)

    return {
        "baseline": {
            "cost_pln": baseline_cost_pln,
            "grid_kwh": baseline_grid_kwh,
            "cost_per_kwh": round(baseline_cost_per_kwh, 6),
            "final_soc_pct": last_soc_pct,
        },
        "points": points,
    }


def render_sensitivity(prices, dummy_loads_kw, initial_soc):
    """Render sensitivity analysis as ASCII bar chart."""
    data = run_sensitivity(prices, dummy_loads_kw, initial_soc)
    baseline = data["baseline"]
    points = data["points"]

    if not points:
        print("No valid results from sensitivity analysis.", file=sys.stderr)
        return ""

    # Find the point matching the natural optimal SOC (highlight in green)
    best_target_pct = baseline["final_soc_pct"]
    best_idx = None
    for i, p in enumerate(points):
        if abs(p["target_soc"] - best_target_pct) < 2.5:  # within ±2.5% tolerance
            best_idx = i
            break

    lines = []
    sep = "─" * 80

    lines.append(f"\n  📊 Sensitivity Analysis — Grid Cost vs Target SOC")
    lines.append(sep)
    lines.append(
        f"  {'Target SOC':>12} │ {'Δ Grid Cost (PLN)':>17} │ {'Δ Cost/kWh':>12} │ Bar"
    )
    lines.append("  " + "─" * 12 + "┼" + "─" * 17 + "┼" + "─" * 12 + "┼" + "─" * 30)

    max_bar_width = 30

    for i, p in enumerate(points):
        target_str = f"{p['target_soc']:2d}%"
        delta_cost_str = f"{p['delta_cost_pln']:+.4f}"
        delta_cpkwh_str = f"{p['delta_cost_per_kwh']:+.6f}"

        # Bar length proportional to absolute delta (normalize to max)
        max_delta = max(abs(p["delta_cost_pln"]) for p in points) or 1
        bar_len = int(abs(p["delta_cost_pln"]) / max_delta * max_bar_width)
        bar_len = max(0, min(bar_len, max_bar_width))

        if i == best_idx:
            color = "\033[92m"  # green for natural optimal SOC
            marker = " ★"
        else:
            color = "\033[90m"  # gray for others
            marker = ""

        bar = "█" * bar_len + "." * (max_bar_width - bar_len)
        reset = "\033[0m"

        line = f"  {color}{target_str:>12}{reset} │ {delta_cost_str:>17} │ {delta_cpkwh_str:>12} │{color}{bar}{reset}{marker}"
        lines.append(line)

    lines.append(sep)
    lines.append(f"\n  ★ = Natural optimal SOC (from unconstrained optimization)")
    if best_target_pct is not None:
        lines.append(
            f"  Baseline cost: {baseline['cost_pln']:+.4f} PLN ({baseline['cost_per_kwh']:.6f} PLN/kWh) at {best_target_pct:.0f}% SOC"
        )
    else:
        lines.append(f"  Baseline cost: {baseline['cost_pln']:+.4f} PLN")
    lines.append("")

    return "\n".join(lines)


def render_sensitivity_json(prices, dummy_loads_kw, initial_soc):
    """Render sensitivity analysis as JSON."""
    data = run_sensitivity(prices, dummy_loads_kw, initial_soc)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
