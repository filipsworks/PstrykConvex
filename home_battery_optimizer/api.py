"""Home Assistant API client for prices, battery state, and dummy loads."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


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
                 'available' tries today first, falls back to tomorrow.

    Returns:
        List of 24 dicts with keys: hour, price (PLN/kWh), is_cheap, is_expensive.
    """
    # Try the "today" sensor first
    url = f"{base_url}/states/sensor.pstryk_aio_obecna_cena_zakupu_pradu"
    resp = requests.get(url, headers=_get_headers(base_url, token), timeout=15)
    resp.raise_for_status()
    data = resp.json()

    prices_attr = data.get("attributes", {})
    is_estimated = False  # default: not estimated

    if horizon == "today":
        raw_prices = prices_attr.get("today_prices", [])
    elif horizon == "tomorrow":
        is_estimated = False
        raw_prices = prices_attr.get("tomorrow_prices", [])
        # If today's sensor doesn't have tomorrow_prices, try the dedicated sensor
        if not raw_prices:
            url2 = f"{base_url}/states/sensor.pstryk_aio_cena_zakupu_pradu_jutro"
            resp2 = requests.get(
                url2, headers=_get_headers(base_url, token), timeout=15
            )
            resp2.raise_for_status()
            data2 = resp2.json()
            raw_prices = data2.get("attributes", {}).get("tomorrow_prices", [])

        # If still no tomorrow data, clone today's prices as estimation fallback
        if not raw_prices:
            is_estimated = True
            raw_prices = prices_attr.get("today_prices", [])
    else:  # "available"
        raw_prices = prices_attr.get("today_prices", [])
        if not raw_prices:
            url2 = f"{base_url}/states/sensor.pstryk_aio_cena_zakupu_pradu_jutro"
            resp2 = requests.get(
                url2, headers=_get_headers(base_url, token), timeout=15
            )
            resp2.raise_for_status()
            data2 = resp2.json()
            raw_prices = data2.get("attributes", {}).get("tomorrow_prices", [])

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

    # Sort by hour and fill any missing hours with average
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

    return full


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
    with time-of-day pattern variation.

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
    readings = []
    for entity_data in data:
        if not isinstance(entity_data, list):
            continue
        for entry in entity_data:
            state = entry.get("state")
            if state in ("unavailable", "unknown"):
                continue
            try:
                readings.append(float(state))
            except (ValueError, TypeError):
                continue

    if not readings:
        return [1.5] * 24

    import statistics

    baseline = statistics.median(readings)

    # Apply time-of-day pattern based on typical home usage
    loads = []
    for h in range(24):
        if 6 <= h <= 8 or 17 <= h <= 20:
            loads.append(baseline * 1.5)  # peak hours
        elif 9 <= h <= 16:
            loads.append(baseline * 1.2)  # daytime elevated
        else:
            loads.append(baseline)  # night baseline

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
