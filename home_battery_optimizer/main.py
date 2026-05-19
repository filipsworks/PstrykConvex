#!/usr/bin/env python3
"""Home battery charging optimizer — CLI entry point with TUI / JSON output."""

import json
from datetime import date, datetime, timezone

import click
from api import fetch_all_data
from battery_model import voltage_to_soc
from optimizer import OBJECTIVE_MIN_COST, OBJECTIVE_MIN_COST_PER_KWH, VALID_OBJECTIVES, optimize
from overrides import OptimizerOverrides, build_overrides_from_query

try:
    from zoneinfo import ZoneInfo

    WARSAW_TZ = ZoneInfo("Europe/Warsaw")
except ImportError:
    # Fallback for Python < 3.9
    WARSAW_TZ = timezone.utc

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


@click.command(
    context_settings={"max_content_width": 120},
    epilog=(
        "Mode legend:\n"
        "  SUB/SNU — charging battery from grid\n"
        "  SBU/OSO — discharging battery to loads"
    ),
)
@click.option("--mock", is_flag=True, help="Use sample data instead of API")
@click.option(
    "--ha-url",
    default="https://ha.kompfix.pl",
    show_default=True,
    help="Home Assistant base URL (e.g. https://ha.kompfix.pl). /api is appended automatically.",
)
@click.option(
    "--ha-token",
    default="",
    show_default=False,
    help="Long-lived access token (required unless --mock)",
)
@click.option(
    "--horizon",
    type=click.Choice(["today", "tomorrow", "available"]),
    default="available",
    show_default=True,
    help="Which day's prices to optimize for (default: available)",
)
@click.option(
    "--days",
    type=int,
    default=7,
    show_default=True,
    help=(
        "Days of history to fetch for the weekday×hour consumption "
        "profile (default: 7, the minimum to cover every weekday)."
    ),
)
@click.option(
    "--output",
    type=click.Choice(["tui", "json", "sensitivity", "sensitivity-json"]),
    default="tui",
    show_default=True,
    help="Output format (default: tui). 'sensitivity'/'sensitivity-json' shows grid cost vs target SOC.",
)
@click.option(
    "--target-soc",
    type=float,
    default=None,
    metavar="PCT",
    help="Target end-of-day SOC in percent (0–100). Default: no constraint.",
)
@click.option(
    "--objective",
    type=click.Choice(["min_cost", "min_cost_per_kwh"]),
    default="min_cost",
    show_default=True,
    help=(
        "Optimisation objective: 'min_cost' minimises total PLN spend; "
        "'min_cost_per_kwh' minimises average PLN/kWh by sweeping EOD SOC targets."
    ),
)
@click.option("--max-soc-pct", type=float, default=95.0, show_default=True,
              help="Upper SOC bound in percent (GBB-style buffer; lower = more headroom).")
@click.option("--min-soc-pct", type=float, default=10.0, show_default=True,
              help="Lower SOC bound in percent (depth-of-discharge floor).")
@click.option("--full-charge-days", default="", metavar="CSV",
              help="Day-of-month CSV (e.g. '1,15') forced to MaxSOC=100% for a balance cycle.")
@click.option("--per-date-target-soc", default="", metavar="LIST",
              help="Per-date EOD SOC pins, 'YYYY-MM-DD:pct,YYYY-MM-DD:pct'.")
@click.option("--consumption-scale", type=float, default=1.0, show_default=True,
              help="Multiplier on the auto-learned consumption profile.")
@click.option("--consumption-profile", default="", metavar="JSON",
              help='Replacement weekday map, JSON: {"0": [24 kW], "1": [...], ...}.')
@click.option("--load-override-kw", default="", metavar="CSV",
              help="Hard replace net load: CSV of 24 (or 48) kW values.")
@click.option("--extra-loads", default="", metavar="LIST",
              help="One-shot loads, 'hour:kw[:duration_h],hour:kw' or JSON list.")
@click.option("--solar-scale", type=float, default=1.0, show_default=True,
              help="PV forecast correction factor (default 1.0).")
@click.option("--solar-override-kwh", default="", metavar="CSV",
              help="Replace PV forecast: CSV of 24 (or 48) kWh values.")
@click.option("--dry-run", is_flag=True,
              help="Test mode: decisions are advisory, not for inverter execution.")
@click.pass_context
def main(
    ctx, mock, ha_url, ha_token, horizon, days, output, target_soc, objective,
    max_soc_pct, min_soc_pct, full_charge_days, per_date_target_soc,
    consumption_scale, consumption_profile, load_override_kw, extra_loads,
    solar_scale, solar_override_kwh, dry_run,
):
    r"""Home battery charging optimizer.

    Examples:

      # Mock run (no API):
      %(prog)s --mock

      # Live data, today's prices, TUI output:
      %(prog)s --ha-url https://ha.example.com --ha-token YOUR_TOKEN

      # JSON output for tomorrow, last 3 days of history:
      %(prog)s --ha-url ... --ha-token ... --horizon tomorrow --days 3 --output json
    """
    # Validate: either --mock or both --ha-url and --ha-token required
    if not mock and not ha_token:
        click.echo(
            "[error] Either --mock or --ha-token is required. Use --help for usage.",
            err=True,
        )
        ctx.exit(1)

    # Build overrides from CLI flags.  Using ``build_overrides_from_query``
    # keeps CLI and REST behaviour identical.
    try:
        overrides = build_overrides_from_query({
            "max_soc_pct": str(max_soc_pct),
            "min_soc_pct": str(min_soc_pct),
            "full_charge_days": full_charge_days,
            "per_date_target_soc": per_date_target_soc,
            "consumption_scale": str(consumption_scale),
            "consumption_profile": consumption_profile,
            "load_override_kw": load_override_kw,
            "extra_loads": extra_loads,
            "solar_scale": str(solar_scale),
            "solar_override_kwh": solar_override_kwh,
            "dry_run": "true" if dry_run else "false",
        })
    except ValueError as e:
        click.echo(f"[error] Bad override: {e}", err=True)
        ctx.exit(1)

    # Build a simple namespace-like object to keep get_data / downstream code unchanged
    args = type(
        "Args",
        (),
        {
            "mock": mock,
            "ha_url": ha_url,
            "ha_token": ha_token,
            "horizon": horizon,
            "days": days,
            "output": output,
            "target_soc": target_soc,
            "objective": objective,
            "overrides": overrides,
        },
    )()

    # Fetch data (now thread overrides through to the API)
    prices, dummy_loads, initial_soc, horizon_dates = get_data(args)

    # Warn if tomorrow's pricing is estimated (cloned from today)
    if _has_estimated_prices(prices):
        click.echo(
            "[warn] Tomorrow's pricing data unavailable — using today's prices as estimation",
            err=True,
        )

    # Calculate current hour in Europe/Warsaw timezone for live runs.
    # The optimizer cannot make decisions for past hours.
    # Only "today" has past hours; "tomorrow"/"available" are fully future.
    if args.mock:
        start_hour = 0
    elif args.horizon == "today":
        now_warsaw = datetime.now(WARSAW_TZ)
        start_hour = now_warsaw.hour
        click.echo(
            f"  Current time (Warsaw): {now_warsaw.strftime('%H:%M')} → "
            f"optimizing from hour {start_hour} onwards",
            err=True,
        )
    else:
        # Tomorrow or available — all hours are in the future
        start_hour = 0

    # Run optimization for each day in the horizon
    all_results = []
    n_days = len(prices) // 24
    if args.horizon == "today":
        n_days = 1
    elif args.horizon == "tomorrow" and n_days >= 2:
        # Skip today, only optimize tomorrow
        prices = prices[24:]
        if len(dummy_loads) >= 48:
            dummy_loads = dummy_loads[24:]
        n_days = 1

    # If we trimmed today out for `--horizon tomorrow`, also trim the date list
    # so per-date overrides still line up with each day.
    block_dates = list(horizon_dates) if horizon_dates else []
    if args.horizon == "tomorrow" and len(block_dates) >= 2:
        block_dates = block_dates[1:]

    for i in range(n_days):
        start = i * 24
        end = min(start + 24, len(prices))
        if start >= end:
            break
        day_prices = prices[start:end]
        # dummy_loads is now horizon-aligned (same length as prices). For mock,
        # it's a single 24h list — repeat it to match longer horizons.
        if len(dummy_loads) == 24 and len(prices) > 24:
            day_loads = dummy_loads
        else:
            day_loads = dummy_loads[start:end]

        soc_start = (
            initial_soc
            if i == 0
            else all_results[-1]["summary"]["final_soc"] / 100.0
            if all_results
            else initial_soc
        )

        # Resolve per-day calendar date so MaxSOC scheduling + per-date target
        # SOC pins from --full-charge-days / --per-date-target-soc apply.
        day_obj = None
        if i < len(block_dates):
            try:
                day_obj = date.fromisoformat(block_dates[i])
            except (ValueError, TypeError):
                day_obj = None

        day_max_soc = (
            overrides.max_soc_for_date(day_obj)
            if day_obj is not None
            else overrides.max_soc_pct / 100.0
        )
        day_min_soc = overrides.min_soc()
        day_target_soc = overrides.target_soc_for_date(
            day_obj, fallback_pct=args.target_soc
        )

        # For the first day, skip past hours; for subsequent days optimize full day
        day_start_hour = start_hour if i == 0 else 0

        try:
            result = optimize(
                day_prices,
                day_loads,
                soc_start,
                target_soc=day_target_soc,
                start_hour=day_start_hour,
                objective=args.objective,
                max_soc=day_max_soc,
                min_soc=day_min_soc,
            )
        except Exception as e:
            click.echo(f"[error] Optimization failed for day {i + 1}: {e}", err=True)
            ctx.exit(1)

        # Inject charge_kwh (signed) and day into each decision
        for d in result["decisions"]:
            d["charge_kwh"] = round(d["charge_wh"] / 1000 - d["discharge_wh"] / 1000, 4)
            d["day"] = i + 1

        # Propagate estimated pricing flag into results (for JSON output)
        if _has_estimated_prices(day_prices):
            result["is_estimated"] = True

        # Annotate full-charge / advisory status so JSON consumers can branch on it.
        result["is_full_charge_day"] = day_max_soc >= 0.999
        result["advisory"] = overrides.dry_run
        if day_obj is not None:
            result["date"] = day_obj.isoformat()

        all_results.append(result)

    # Output
    if args.output == "json":
        click.echo(render_json(all_results, overrides=overrides))
    elif args.output == "sensitivity":
        click.echo(render_sensitivity(prices, dummy_loads, initial_soc))
    elif args.output == "sensitivity-json":
        render_sensitivity_json(prices, dummy_loads, initial_soc)
    else:
        if overrides.dry_run:
            click.echo("[dry-run] Advisory output — do not push decisions to inverter.", err=True)
        click.echo(render_tui(all_results, horizon=args.horizon))


# ── Data fetching ──────────────────────────────────────────────────────────


def _build_base_url(ha_url: str) -> str:
    """Ensure base URL ends with /api."""
    url = ha_url.rstrip("/")
    if not url.endswith("/api"):
        url += "/api"
    return url


def get_data(args):
    """Return (prices, dummy_loads, initial_soc, horizon_dates) for the requested horizon.

    ``args.overrides`` is forwarded to :func:`api.fetch_all_data` so the
    returned ``dummy_loads`` already reflects consumption/solar overrides.
    ``horizon_dates`` is a list of ISO date strings (one per 24-hour block) so
    callers can resolve per-date overrides (full-charge days, target SOC pins).
    """
    overrides = getattr(args, "overrides", None)
    horizon_dates: list[str] = []

    if args.mock:
        click.echo("[mock] Using sample data", err=True)
        prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
        dummy_loads = MOCK_DUMMY_LOADS
        initial_soc = 0.35
        # Mock data is single-day; assign today's date for per-date overrides.
        horizon_dates = [datetime.now(WARSAW_TZ).date().isoformat()]
        # Apply override-driven scale + extras to mock loads so dry-runs work.
        if overrides is not None:
            scaled = [v * overrides.consumption_scale for v in dummy_loads]
            for load in overrides.extra_loads:
                base = load.get("day", 0) * 24
                for off in range(load["duration_h"]):
                    idx = base + load["hour"] + off
                    if 0 <= idx < len(scaled):
                        scaled[idx] += load["kw"]
            if overrides.load_override_kw is not None:
                for i in range(min(len(scaled), len(overrides.load_override_kw))):
                    scaled[i] = max(0.0, overrides.load_override_kw[i])
            dummy_loads = [round(v, 3) for v in scaled]
    else:
        base_url = _build_base_url(args.ha_url)

        click.echo(f"Fetching data from {base_url} ...", err=True)
        try:
            import api as api_mod

            data = api_mod.fetch_all_data(
                base_url=base_url,
                token=args.ha_token,
                days=args.days,
                horizon=args.horizon,
                overrides=overrides,
            )
        except Exception as e:
            click.echo(f"[error] API failed ({e}), falling back to mock", err=True)
            prices = sorted(MOCK_PRICES, key=lambda p: p["hour"])
            dummy_loads = MOCK_DUMMY_LOADS
            initial_soc = 0.35
            horizon_dates = [datetime.now(WARSAW_TZ).date().isoformat()]
        else:
            prices = data["prices"]
            dummy_loads = data["dummy_loads"]
            horizon_dates = data.get("horizon_dates") or []
            voltage = data.get("voltage")
            if voltage is not None:
                initial_soc = voltage_to_soc(voltage)
                click.echo(
                    f"  Voltage {voltage:.1f} V → SOC {initial_soc * 100:.1f}%",
                    err=True,
                )
            else:
                initial_soc = 0.5
                click.echo("  No voltage reading — defaulting to SOC 50%", err=True)

    return prices, dummy_loads, initial_soc, horizon_dates


def _has_estimated_prices(prices: list[dict]) -> bool:
    """Check if any price entry is marked as estimated (cloned from today)."""
    return any(p.get("is_estimated", False) for p in prices)


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
            if ch_kwh > 0:
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
        grid_kwh = summary['total_grid_kwh']
        if grid_kwh > 0:
            lines.append(f"    Cost/kWh:      {summary['total_cost_pln'] / grid_kwh:>8.4f} PLN/kWh")
        obj_label = result.get('objective', 'min_cost')
        if obj_label == 'min_cost_per_kwh' and result.get('chosen_target_soc_pct') is not None:
            lines.append(f"    Objective:     min_cost_per_kwh  (target SOC {result['chosen_target_soc_pct']:.0f}%,  {result['chosen_cost_per_kwh']:.4f} PLN/kWh)")
        else:
            lines.append(f"    Objective:     {obj_label}")

    # Legend (once at end)
    if all_results:
        lines.append("")
        lines.append(sep)
        lines.append("  Mode legend:")
        lines.append(
            "    SUB/SNU — charging battery from grid              [green bar]"
        )
        lines.append(
            "    SBU/OSO — discharging battery to loads             [blue bar]"
        )
        lines.append("")

    return "\n".join(lines)


# ── JSON rendering ─────────────────────────────────────────────────────────


def render_json(all_results: list[dict], overrides: OptimizerOverrides | None = None) -> str:
    """Return results as pretty-printed JSON, with optional overrides summary."""
    if overrides is None:
        return json.dumps(all_results, indent=2)
    payload = {
        "advisory": overrides.dry_run,
        "overrides_active": overrides.summary(),
        "days": all_results,
    }
    return json.dumps(payload, indent=2)


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
    click.echo("Running unconstrained optimization (baseline)...", err=True)
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
        click.echo(f"  [warn] Baseline optimization failed: {e}", err=True)
        baseline_cost_pln = 0
        baseline_grid_kwh = 1
        baseline_cost_per_kwh = 0
        last_soc_pct = None

    # Now run sensitivity for each target SOC level
    points = []
    soc_levels = range(0, 105, 5)  # 0%, 5%, ..., 100%

    click.echo(
        f"Running sensitivity analysis ({len(soc_levels)} scenarios)...",
        err=True,
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
            click.echo(f"  [warn] Target SOC {target_pct}% failed: {e}", err=True)

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
        click.echo("No valid results from sensitivity analysis.", err=True)
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
    click.echo(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
