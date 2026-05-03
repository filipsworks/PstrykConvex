"""Home Assistant API client for prices, battery state, and dummy loads."""

import requests
from datetime import datetime, timezone, timedelta
from typing import Optional


HA_BASE_URL = "https://ha-finland.kompfix.pl/api"
# Use env var or placeholder — user should set HA_TOKEN before running
HA_TOKEN = ""


def _headers() -> dict:
    return {"Authorization": f"Bearer {HA_TOKEN}"}


def fetch_hourly_prices(date_str: str) -> list[dict]:
    """Fetch hourly prices for a given date (YYYY-MM-DD).

    Tries today_prices first, falls back to tomorrow_prices.
    Returns list of dicts with keys: hour, price (PLN/kWh), is_cheap, is_expensive.
    """
    url = f"{HA_BASE_URL}/states/sensor.pstryk_aio_obecna_cena_zakupu_pradu"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # Try today first
    prices = data.get("attributes", {}).get("today_prices", [])
    if not prices:
        url2 = f"{HA_BASE_URL}/states/sensor.pstryk_aio_cena_zakupu_pradu_jutro"
        resp2 = requests.get(url2, headers=_headers(), timeout=15)
        resp2.raise_for_status()
        data2 = resp2.json()
        prices = data2.get("attributes", {}).get("tomorrow_prices", [])

    result = []
    for p in prices:
        start_dt = datetime.fromisoformat(p["start"])
        hour = start_dt.hour
        result.append({
            "hour": hour,
            "price": float(p["price"]),
            "is_cheap": bool(p.get("is_cheap", False)),
            "is_expensive": bool(p.get("is_expensive", False)),
        })

    # Sort by hour and fill any missing hours with average
    result.sort(key=lambda x: x["hour"])
    avg_price = sum(p["price"] for p in result) / len(result) if result else 1.0

    full = [None] * 24
    for p in result:
        full[p["hour"]] = p
    for h in range(24):
        if full[h] is None:
            full[h] = {"hour": h, "price": avg_price, "is_cheap": False, "is_expensive": False}

    return full


def fetch_battery_voltage() -> Optional[float]:
    """Fetch current battery voltage."""
    url = f"{HA_BASE_URL}/states/sensor.gniazdo_battery_voltage"
    resp = requests.get(url, headers=_headers(), timeout=15)
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


def fetch_battery_currents() -> Optional[dict]:
    """Fetch current charge and discharge currents."""
    url = (
        f"{HA_BASE_URL}/history/period/"
        f"2026-05-03T14:00:00.000Z?"
        f"filter_entity_id=sensor.gniazdo_battery_charging_current,"
        f"sensor.gniazdo_battery_discharge_current&minimal_response=true"
    )
    resp = requests.get(url, headers=_headers(), timeout=15)
    if resp.status_code != 200:
        return None

    data = resp.json()
    result = {"charging": 0.0, "discharging": 0.0}
    for entity_data in data:
        if not isinstance(entity_data, list):
            continue
        # Get the latest non-unavailable reading
        for entry in reversed(entity_data):
            state = entry.get("state")
            if state in ("unavailable", "unknown"):
                continue
            try:
                val = float(state)
            except (ValueError, TypeError):
                continue

            entity_id = entry.get("entity_id", "")
            if "charging_current" in entity_id:
                result["charging"] = val
            elif "discharge_current" in entity_id:
                result["discharging"] = val
            break  # take latest per entity

    return result


def fetch_dummy_loads() -> list[float]:
    """Fetch recent dummy load readings (kW) from inverter output power.

    Returns a list of 24 estimated kW values — uses the median of recent
    readings as baseline, with slight variation by time-of-day pattern.
    """
    url = (
        f"{HA_BASE_URL}/history/period/"
        f"2026-05-03T00:00:00.000Z?"
        f"filter_entity_id=sensor.gniazdo_output_active_power&minimal_response=true"
    )
    resp = requests.get(url, headers=_headers(), timeout=15)
    if resp.status_code != 200:
        return [0.15] * 24  # fallback

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
        return [0.15] * 24

    # Use median as baseline dummy load
    import statistics
    baseline = statistics.median(readings)

    # Apply a simple time-of-day pattern: slightly higher during day
    loads = []
    for h in range(24):
        if 9 <= h <= 14:
            loads.append(baseline * 1.5)   # charging hours — dummy load includes charger overhead
        else:
            loads.append(baseline)

    return loads


def fetch_all_data() -> dict:
    """Fetch all required data in one call."""
    prices = fetch_hourly_prices(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    voltage = fetch_battery_voltage()
    currents = fetch_battery_currents()
    dummy_loads = fetch_dummy_loads()

    return {
        "prices": prices,
        "voltage": voltage,
        "currents": currents,
        "dummy_loads": dummy_loads,
    }
