"""Home Assistant API client for prices, battery state, and dummy loads."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

def _get_headers(base_url: str, token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fetch_hourly_prices(
    base_url: str, token: str, horizon: str = "available"
) -> list[dict]:
    """Fetch hourly prices for the requested horizon.

    Args:
        base_url: HA API base URL (e.g. https://ha-finland.kompfix.pl/api).
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
    """Fetch battery voltage and current history for the last N days.

    Returns dict with 'voltage' (latest), 'charging', 'discharging' lists of dicts.
    """
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


def fetch_dummy_loads(base_url: str, token: str, days: int = 1) -> list[float]:
    """Fetch recent dummy load readings (kW) from inverter output power.

    Returns a list of 24 estimated kW values based on median of recent readings,
    grouped by actual hour-of-day to capture real usage patterns.

    Args:
        base_url: HA API base URL.
        token: Long-lived access token.
        days: How many days of history to sample.
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
        return [1.5] * 24  # fallback

    data = resp.json()

    # Group readings by hour-of-day (UTC). Each entry has a "last_changed" ISO timestamp.
    hourly_readings: dict[int, list[float]] = {h: [] for h in range(24)}
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

            # Extract hour from the timestamp
            ts = entry.get("last_changed", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local_hour = dt.astimezone(WARSAW_TZ).hour
                if 0 <= local_hour < 24:
                    hourly_readings[local_hour].append(value)
            except (ValueError, TypeError):
                # If we can't parse the timestamp, skip hour grouping but keep value
                pass

    if not all_values:
        return [1.5] * 24

    import statistics

    overall_median = statistics.median(all_values)

    # Build 24h profile: use per-hour median where data exists, fallback to overall median
    loads = []
    for h in range(24):
        hour_readings = hourly_readings[h]
        if hour_readings:
            loads.append(round(statistics.median(hour_readings), 3))
        else:
            # No data for this hour — use overall median as fallback
            loads.append(round(overall_median, 3))

    return loads


def fetch_all_data(
    base_url: str, token: str, days: int = 1, horizon: str = "available"
) -> dict:
    """Fetch all required data in one call.

    Args:
        base_url: HA API base URL (e.g. https://ha-finland.kompfix.pl/api).
        token: Long-lived access token.
        days: Number of days for history sampling.
        horizon: Which day's prices to use ('today', 'tomorrow', 'available').
    """
    prices = fetch_hourly_prices(base_url, token, horizon=horizon)
    voltage = fetch_battery_voltage(base_url, token)
    battery_hist = fetch_battery_history(base_url, token, days=days)
    dummy_loads = fetch_dummy_loads(base_url, token, days=days)

    return {
        "prices": prices,
        "voltage": voltage,
        "battery_history": battery_hist,
        "dummy_loads": dummy_loads,
    }
