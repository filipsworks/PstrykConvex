"""Home Assistant API client for prices, battery state, dummy loads,
solar forecast, and Polish out-of-work days."""

import re
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from overrides import OptimizerOverrides

try:
    from zoneinfo import ZoneInfo

    WARSAW_TZ = ZoneInfo("Europe/Warsaw")
except ImportError:
    WARSAW_TZ = timezone.utc  # Python < 3.9 fallback


# In-process cache for the Polish holiday scrape (one HTTP call per year per run).
_HOLIDAYS_CACHE: dict[int, set[str]] = {}


def _get_headers(base_url: str, token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Pricing ────────────────────────────────────────────────────────────────


def fetch_hourly_prices(
    base_url: str, token: str, horizon: str = "available"
) -> list[dict]:
    """Fetch hourly prices for the requested horizon.

    Args:
        base_url: HA API base URL (e.g. https://ha.kompfix.pl/api).
        token: Long-lived access token.
        horizon: 'today', 'tomorrow', or 'available'.
                 'available' returns today + tomorrow if both are available,
                 otherwise just the day that has data.

    Returns:
        List of 24 or 48 dicts with keys: hour, price (PLN/kWh), is_cheap,
        is_expensive, and optionally is_estimated.
    """

    def _parse_price_list(raw_prices, is_estimated):
        result = []
        for p in raw_prices:
            start_dt = datetime.fromisoformat(p["start"])
            hour = start_dt.hour
            entry = {
                "hour": hour,
                "price": float(p["price"]),
                "is_cheap": bool(p.get("is_cheap", False)),
                "is_expensive": bool(p.get("is_expensive", False)),
            }
            if is_estimated:
                entry["is_estimated"] = True
            result.append(entry)

        result.sort(key=lambda x: x["hour"])
        avg_price = sum(p["price"] for p in result) / len(result) if result else 1.0

        full = [None] * 24
        for p in result:
            full[p["hour"]] = p
        for h in range(24):
            if full[h] is None:
                full[h] = {
                    "hour": h,
                    "price": avg_price,
                    "is_cheap": False,
                    "is_expensive": False,
                }

        return full, is_estimated

    def _fetch_from_main(attr_key):
        try:
            url = f"{base_url}/states/sensor.pstryk_aio_obecna_cena_zakupu_pradu"
            resp = requests.get(url, headers=_get_headers(base_url, token), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            raw_prices = data.get("attributes", {}).get(attr_key, [])
            return _parse_price_list(raw_prices, is_estimated=False)
        except Exception:
            return [], True

    def _fetch_tomorrow_sensor():
        try:
            url = f"{base_url}/states/sensor.pstryk_aio_cena_zakupu_pradu_jutro"
            resp = requests.get(url, headers=_get_headers(base_url, token), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            raw_prices = data.get("attributes", {}).get("tomorrow_prices", [])
            if raw_prices:
                return _parse_price_list(raw_prices, is_estimated=False)
        except Exception:
            pass
        return [], True

    if horizon == "today":
        prices, _ = _fetch_from_main("today_prices")
        return prices

    elif horizon == "tomorrow":
        prices, _ = _fetch_tomorrow_sensor()
        if not prices:
            # Fallback: clone today's prices as estimation
            today_prices, _ = _fetch_from_main("today_prices")
            for p in today_prices:
                p["is_estimated"] = True
            return today_prices
        return prices

    else:  # "available" — try to get both today and tomorrow
        today_prices, _ = _fetch_from_main("today_prices")
        if not today_prices:
            # No today data — try tomorrow only
            tomorrow_prices, _ = _fetch_tomorrow_sensor()
            for p in tomorrow_prices:
                p["is_estimated"] = True
            return tomorrow_prices

        tomorrow_prices, _ = _fetch_tomorrow_sensor()
        if not tomorrow_prices:
            # No tomorrow data — just return today
            return today_prices

        # Both available — concatenate (today + tomorrow)
        return today_prices + tomorrow_prices


# ── Battery ────────────────────────────────────────────────────────────────


def fetch_battery_voltage(base_url: str, token: str) -> Optional[float]:
    """Fetch current battery voltage."""
    url = f"{base_url}/states/sensor.gniazdo_battery_voltage"
    resp = requests.get(url, headers=_get_headers(base_url, token), timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()
    state = data.get("state")
    if state in ("unavailable", "unknown"):
        return None
    try:
        return float(state)
    except (ValueError, TypeError):
        return None


def fetch_battery_history(base_url: str, token: str, days: int = 1) -> dict:
    """Fetch battery voltage and current history for the last N days."""
    end_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    start_dt = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    url = (
        f"{base_url}/history/period/{start_dt}?"
        f"end_time={end_dt}&"
        f"filter_entity_id=sensor.gniazdo_battery_voltage,"
        f"sensor.gniazdo_battery_charging_current,"
        f"sensor.gniazdo_battery_discharge_current&minimal_response=true"
    )
    resp = requests.get(url, headers=_get_headers(base_url, token), timeout=30)
    if resp.status_code != 200:
        return {"voltage": None, "charging": [], "discharging": []}

    data = resp.json()
    result = {"voltage": None, "charging": [], "discharging": []}

    for entity_data in data:
        if not isinstance(entity_data, list):
            continue
        entity_id = entity_data[0].get("entity_id", "") if entity_data else ""

        for entry in entity_data:
            state = entry.get("state")
            if state in ("unavailable", "unknown"):
                continue
            try:
                val = float(state)
            except (ValueError, TypeError):
                continue

            ts = entry.get("last_changed", "")
            point = {"time": ts, "value": val}

            if "voltage" in entity_id:
                result["voltage"] = val  # keep latest
            elif "charging_current" in entity_id:
                result["charging"].append(point)
            elif "discharge_current" in entity_id:
                result["discharging"].append(point)

    return result


# ── Consumption (inverter output) ──────────────────────────────────────────


def fetch_consumption_by_weekday(
    base_url: str, token: str, days: int = 7
) -> dict:
    """Fetch inverter output power and bucket it by (weekday, hour-of-day).

    Source sensor: ``sensor.gniazdo_output_active_power`` (inverter total output
    in kW). Per-hour median is computed per weekday over the past ``days`` of
    history so that workdays and weekends end up with distinct profiles.

    Args:
        base_url: HA API base URL.
        token: Long-lived access token.
        days: How many days of history to sample (default 7 so every weekday
              gets at least one observation).

    Returns:
        dict with:
          - profiles: ``{weekday(0..6): [24 floats]}`` — kW per hour, weekday is
            Monday=0..Sunday=6 (Python `datetime.weekday()`).
          - overall_hourly: ``[24 floats]`` — per-hour median across *all*
            sampled days (used as fallback when a (weekday, hour) bucket is
            empty).
          - overall_median: float — single median over every reading (last
            fallback if even ``overall_hourly[h]`` is missing).
    """
    end_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    start_dt = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    url = (
        f"{base_url}/history/period/{start_dt}?"
        f"end_time={end_dt}&"
        f"filter_entity_id=sensor.gniazdo_output_active_power&minimal_response=true"
    )
    resp = requests.get(url, headers=_get_headers(base_url, token), timeout=30)
    if resp.status_code != 200:
        return _empty_consumption_profile()

    data = resp.json()

    # Bucket by (weekday, hour-of-day) in Warsaw local time.
    by_wd_hr: dict[tuple[int, int], list[float]] = {
        (wd, h): [] for wd in range(7) for h in range(24)
    }
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    all_values: list[float] = []

    for entity_data in data:
        if not isinstance(entity_data, list):
            continue
        for entry in entity_data:
            state = entry.get("state")
            if state in ("unavailable", "unknown"):
                continue
            try:
                value = float(state)
            except (ValueError, TypeError):
                continue

            all_values.append(value)

            ts = entry.get("last_changed", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local = dt.astimezone(WARSAW_TZ)
                wd = local.weekday()
                hr = local.hour
                if 0 <= hr < 24 and 0 <= wd < 7:
                    by_wd_hr[(wd, hr)].append(value)
                    by_hour[hr].append(value)
            except (ValueError, TypeError):
                continue

    if not all_values:
        return _empty_consumption_profile()

    overall_median = statistics.median(all_values)

    overall_hourly = []
    for h in range(24):
        if by_hour[h]:
            overall_hourly.append(round(statistics.median(by_hour[h]), 3))
        else:
            overall_hourly.append(round(overall_median, 3))

    profiles: dict[int, list[float]] = {}
    for wd in range(7):
        row = []
        for h in range(24):
            samples = by_wd_hr[(wd, h)]
            if samples:
                row.append(round(statistics.median(samples), 3))
            else:
                row.append(overall_hourly[h])
        profiles[wd] = row

    return {
        "profiles": profiles,
        "overall_hourly": overall_hourly,
        "overall_median": round(overall_median, 3),
    }


def _empty_consumption_profile() -> dict:
    fallback = [1.5] * 24
    return {
        "profiles": {wd: list(fallback) for wd in range(7)},
        "overall_hourly": list(fallback),
        "overall_median": 1.5,
    }


# ── Solar forecast ─────────────────────────────────────────────────────────


def _parse_wh_period(wh_period: dict) -> list[float]:
    """Convert a Solar.Forecast ``wh_period`` attribute into a 24-slot kWh list.

    The attribute keys are ISO timestamps at the *hour boundary* in local time
    (Europe/Warsaw with offset) — one per hour of the relevant day.  We aggregate
    by Warsaw local hour to produce kWh per hour-of-day.
    """
    hourly_wh = [0.0] * 24
    for ts_str, wh in wh_period.items():
        try:
            dt = datetime.fromisoformat(ts_str)
            local = dt.astimezone(WARSAW_TZ) if dt.tzinfo else dt
            hr = local.hour
            if 0 <= hr < 24:
                hourly_wh[hr] += float(wh)
        except (ValueError, TypeError):
            continue
    return [round(v / 1000.0, 4) for v in hourly_wh]  # Wh → kWh


def fetch_solar_forecast(
    base_url: str, token: str
) -> dict:
    """Fetch per-hour solar production forecast for today and tomorrow.

    Sources:
      - ``sensor.dom_energy_production_today``    (attribute ``wh_period``)
      - ``sensor.dom_energy_production_tomorrow`` (attribute ``wh_period``)

    Returns:
        dict with ``today`` and ``tomorrow`` keys, each a 24-element list of
        kWh per Warsaw local hour. Missing sensors yield a zero-filled day.
    """
    out = {"today": [0.0] * 24, "tomorrow": [0.0] * 24}
    for key, entity in (
        ("today", "sensor.dom_energy_production_today"),
        ("tomorrow", "sensor.dom_energy_production_tomorrow"),
    ):
        try:
            url = f"{base_url}/states/{entity}"
            resp = requests.get(url, headers=_get_headers(base_url, token), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            wh_period = data.get("attributes", {}).get("wh_period", {})
            if wh_period:
                out[key] = _parse_wh_period(wh_period)
        except Exception:
            # Leave the zero-filled fallback in place.
            pass
    return out


# ── Out-of-work days (Polish public holidays + weekends) ───────────────────


def fetch_holidays_pl(year: int) -> set[str]:
    """Scrape https://www.kalendarzswiat.pl/swieta/wolne_od_pracy/<year>.

    The page is plain HTML; each public holiday cell carries a
    ``data-date="YYYY-M-D"`` attribute.  We extract those and normalise to
    ``YYYY-MM-DD``.  Result is cached per-process per-year so each run hits the
    site at most once per requested year.
    """
    if year in _HOLIDAYS_CACHE:
        return _HOLIDAYS_CACHE[year]

    url = f"https://www.kalendarzswiat.pl/swieta/wolne_od_pracy/{year}"
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "home-battery-optimizer/1.0"},
        )
        resp.raise_for_status()
        html = resp.text
    except Exception:
        _HOLIDAYS_CACHE[year] = set()
        return _HOLIDAYS_CACHE[year]

    holidays: set[str] = set()
    for m in re.finditer(r'data-date="(\d{4})-(\d{1,2})-(\d{1,2})"', html):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if int(y) != year:
            continue
        holidays.add(f"{int(y):04d}-{int(mo):02d}-{int(d):02d}")

    _HOLIDAYS_CACHE[year] = holidays
    return holidays


def is_out_of_work(day: date, holidays: set[str]) -> bool:
    """True if ``day`` is Saturday, Sunday, or a Polish public holiday."""
    if day.weekday() >= 5:  # Sat=5, Sun=6
        return True
    return day.isoformat() in holidays


# ── Horizon-aligned assembly ───────────────────────────────────────────────


def _horizon_dates(horizon: str, prices: list[dict]) -> list[date]:
    """Pick the Warsaw-local calendar date for each 24-hour block in ``prices``.

    The Pstryk endpoints don't tag entries with a date, so we infer them from
    the requested horizon and the current Warsaw day.  This is the date assigned
    to *every* hour inside that 24-hour block.
    """
    today = datetime.now(WARSAW_TZ).date()
    n_blocks = max(1, len(prices) // 24)

    if horizon == "today":
        return [today]
    if horizon == "tomorrow":
        return [today + timedelta(days=1)]
    # "available" — first block is today, second (if present) is tomorrow,
    # extra blocks (rare) chain forward.
    return [today + timedelta(days=i) for i in range(n_blocks)]


def _build_net_load(
    prices: list[dict],
    consumption: dict,
    solar: dict,
    horizon_dates: list[date],
    overrides: Optional[OptimizerOverrides] = None,
) -> tuple[list[float], list[float], list[float]]:
    """Assemble per-horizon-hour (net_load, raw_consumption, solar) lists.

    Net load = max(0, consumption[weekday][hour] - solar[date][hour]).

    When an :class:`OptimizerOverrides` is supplied, the following knobs are
    applied IN ORDER so layering is predictable:

      1. ``consumption_profile``  → replace the auto-learned weekday map.
      2. ``consumption_scale``    → multiplier on the per-hour kW.
      3. ``solar_override_kwh``   → full PV forecast replacement.
      4. ``solar_scale``          → multiplier on the PV forecast.
      5. (net = max(0, consumption − solar))
      6. ``extra_loads``          → add planned one-shot loads (kW × duration_h)
      7. ``load_override_kw``     → final hard replacement of the net load.

    Returns three parallel lists, each ``len(prices)`` long.
    """
    profiles: dict[int, list[float]] = consumption["profiles"]
    overall_hourly: list[float] = consumption["overall_hourly"]

    if overrides is None:
        overrides = OptimizerOverrides()

    # (1) Profile replacement.
    if overrides.consumption_profile is not None:
        profiles = {wd: overrides.consumption_profile.get(wd, profiles.get(wd, overall_hourly))
                    for wd in range(7)}

    net: list[float] = []
    raw: list[float] = []
    sol: list[float] = []

    for i, _ in enumerate(prices):
        block_idx = i // 24
        hour = i % 24
        day = (
            horizon_dates[block_idx]
            if block_idx < len(horizon_dates)
            else horizon_dates[-1]
        )

        wd = day.weekday()
        cons_kw = profiles.get(wd, overall_hourly)[hour]
        # (2) Consumption scale.
        cons_kw *= overrides.consumption_scale

        # Pick the right solar day. Block 0 = today's solar, block 1 = tomorrow's.
        if block_idx == 0:
            sol_kwh = solar["today"][hour]
        elif block_idx == 1:
            sol_kwh = solar["tomorrow"][hour]
        else:
            sol_kwh = 0.0

        # (3) Solar override per horizon hour.
        if overrides.solar_override_kwh is not None and i < len(overrides.solar_override_kwh):
            sol_kwh = overrides.solar_override_kwh[i]
        # (4) Solar scale (correction factor).
        sol_kwh *= overrides.solar_scale

        # (5) 1-hour buckets, so kWh ≈ kW for the purposes of subtraction.
        net_kw = max(0.0, cons_kw - sol_kwh)

        raw.append(round(cons_kw, 3))
        sol.append(round(sol_kwh, 3))
        net.append(round(net_kw, 3))

    # (6) Extra one-shot loads (additive).
    for load in overrides.extra_loads:
        day_idx = load.get("day", 0)
        base = day_idx * 24
        for offset in range(load["duration_h"]):
            idx = base + load["hour"] + offset
            if 0 <= idx < len(net):
                net[idx] = round(net[idx] + load["kw"], 3)

    # (7) Final hard load override (replaces net entirely, length-tolerant).
    if overrides.load_override_kw is not None:
        for i in range(min(len(net), len(overrides.load_override_kw))):
            net[i] = round(max(0.0, overrides.load_override_kw[i]), 3)

    return net, raw, sol


# ── One-shot fetcher used by main.py / rest_service.py ─────────────────────


def fetch_all_data(
    base_url: str,
    token: str,
    days: int = 7,
    horizon: str = "available",
    overrides: Optional[OptimizerOverrides] = None,
) -> dict:
    """Fetch everything the optimizer needs in one call.

    Args:
        base_url: HA API base URL (e.g. https://ha.kompfix.pl/api).
        token: Long-lived access token.
        days: Days of history for the weekday × hour consumption profile
              (default 7 — ensures every weekday is represented at least once).
        horizon: 'today', 'tomorrow', or 'available'.
        overrides: Optional :class:`OptimizerOverrides` applied to the
                   consumption profile, PV forecast, and net load.  When
                   omitted, default (no-op) overrides are used so callers
                   that don't care keep working unchanged.

    Returns:
        dict with:
          - prices: list of hourly price dicts (24 or 48 long).
          - voltage: latest battery voltage (or None).
          - battery_history: voltage & current series.
          - dummy_loads: net load per horizon hour (consumption − solar, ≥ 0).
                         Length matches ``prices``.
          - raw_consumption: gross consumption per horizon hour (weekday-keyed).
          - solar_forecast_kwh: per-horizon-hour solar production (kWh).
          - consumption_profiles: full ``{weekday: [24 kW]}`` map.
          - out_of_work_days: list of out-of-work dates (Sat/Sun + PL holidays)
                              covering the horizon.
          - horizon_dates: dates assigned to each 24-hour block.
    """
    if overrides is None:
        overrides = OptimizerOverrides()

    prices = fetch_hourly_prices(base_url, token, horizon=horizon)
    voltage = fetch_battery_voltage(base_url, token)
    battery_hist = fetch_battery_history(base_url, token, days=days)
    consumption = fetch_consumption_by_weekday(base_url, token, days=days)
    solar = fetch_solar_forecast(base_url, token)

    h_dates = _horizon_dates(horizon, prices)

    # Holiday set for every year touched by the horizon (typically 1, rarely 2).
    holidays: set[str] = set()
    for d in h_dates:
        holidays |= fetch_holidays_pl(d.year)

    out_of_work_days = [
        d.isoformat() for d in h_dates if is_out_of_work(d, holidays)
    ]

    net_load, raw_consumption, solar_per_hour = _build_net_load(
        prices, consumption, solar, h_dates, overrides=overrides
    )

    return {
        "prices": prices,
        "voltage": voltage,
        "battery_history": battery_hist,
        "dummy_loads": net_load,
        "raw_consumption": raw_consumption,
        "solar_forecast_kwh": solar_per_hour,
        "consumption_profiles": consumption["profiles"],
        "out_of_work_days": out_of_work_days,
        "horizon_dates": [d.isoformat() for d in h_dates],
    }
