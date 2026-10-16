"""Home Assistant API client for prices, battery state, dummy loads,
solar forecast, and Polish out-of-work days."""

import json
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
from websockets.sync.client import connect as ws_connect

from battery_model import voltage_to_soc
from overrides import OptimizerOverrides

try:
    from zoneinfo import ZoneInfo

    WARSAW_TZ = ZoneInfo("Europe/Warsaw")
except ImportError:
    WARSAW_TZ = timezone.utc  # Python < 3.9 fallback


# ── Home Assistant entity IDs (DessMonitor integration, SandiSolar 11 kW) ──
# Replaced the retired "Solar of Things" ``sensor.gniazdo_*`` entities in 2026-09.
BATTERY_VOLTAGE_ENTITY = (
    "sensor.inverter_e50000254944442369_dessmonitor_battery_voltage"
)
INVERTER_OUTPUT_POWER_ENTITY = (
    "sensor.inverter_e50000254944442369_dessmonitor_output_power"
)
# DessMonitor reports output power in W; the whole optimizer works in kW.
INVERTER_OUTPUT_POWER_TO_KW = 0.001

# Lifetime PV yield counter (kWh, total_increasing): measured production for
# the prediction-vs-reality reconciliation.
PV_ENERGY_TOTAL_ENTITY = "sensor.inverter_e50000254944442369_dessmonitor_energy_total"

# JK BMS (template helpers over sensor.jk_bms). The helper goes "unavailable"
# once sensor.jk_bms has been silent for 60 s (BLE dropouts of ~8 min seen).
BMS_SOC_ENTITY = "sensor.jk_bms_soc"
# Oldest BMS SOC reading still trusted over the voltage estimate. At 80 A the
# pack moves ~1 % every 2 min, so 15 min bounds the error to roughly 7 %.
BMS_SOC_MAX_AGE_MIN = 15

SOLAR_FORECAST_TODAY_ENTITY = "sensor.dom_energy_production_today"
SOLAR_FORECAST_TOMORROW_ENTITY = "sensor.dom_energy_production_tomorrow"
SOLAR_FORECAST_D2_ENTITY = "sensor.dom_energy_production_d2"
# PV made per hour (hourly "change" of the daily counter) and the BMS SOC,
# for the intraday forecast correction.
PV_ENERGY_TODAY_ENTITY = "sensor.inverter_e50000254944442369_dessmonitor_energy_today"

# The "available" horizon is padded with estimated prices (the last known
# day, hour by hour) to at least this many hours from now, so a plan made
# before tomorrow's prices are out still keeps power on through tomorrow.
MIN_HORIZON_H = 36
# Hours whose BMS SOC peaked at or above this are skipped by the PV
# correction: a full pack curtails PV, so the counter under-reads.
PV_CURTAILED_SOC_PCT = 98


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
    url = f"{base_url}/states/{BATTERY_VOLTAGE_ENTITY}"
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


def fetch_bms_soc(
    base_url: str, token: str, max_age_min: int = BMS_SOC_MAX_AGE_MIN
) -> Optional[float]:
    """Latest JK BMS SOC as a 0–1 fraction, or ``None``.

    Reads the last ``max_age_min`` minutes of history rather than the live
    state: HA includes the state at window start, so the last numeric entry is
    either the current value or — while the helper is ``unavailable`` during a
    BLE dropout — the last reading no older than the window.
    """
    start = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    url = (
        f"{base_url}/history/period/{start}?"
        f"filter_entity_id={BMS_SOC_ENTITY}&minimal_response=true&no_attributes=true"
    )
    try:
        resp = requests.get(url, headers=_get_headers(base_url, token), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    if not data or not isinstance(data[0], list):
        return None
    for entry in reversed(data[0]):
        try:
            return max(0.0, min(1.0, float(entry.get("state")) / 100.0))
        except (ValueError, TypeError):
            continue  # "unavailable" / "unknown"
    return None


def fetch_initial_soc(base_url: str, token: str) -> tuple[float, str]:
    """Starting SOC (0–1) and its source: ``bms``, ``voltage`` or ``default``.

    Pack voltage is a poor LiFePO4 SOC proxy — flat curve, inflated while
    charging (53.2 V maps to 65 % while the BMS reported 20 %) — so it is only
    used when the BMS has been silent for longer than ``BMS_SOC_MAX_AGE_MIN``.
    """
    soc = fetch_bms_soc(base_url, token)
    if soc is not None:
        return soc, "bms"
    voltage = fetch_battery_voltage(base_url, token)
    if voltage is not None:
        return voltage_to_soc(voltage), "voltage"
    return 0.5, "default"


# ── Consumption (inverter output) ──────────────────────────────────────────


def fetch_statistics(
    base_url: str,
    token: str,
    statistic_ids: list[str],
    start: datetime,
    end: datetime,
    types: tuple[str, ...] = ("mean",),
    period: str = "hour",
) -> dict[str, list[dict]]:
    """Long-term statistics through HA's WebSocket API (REST has none).

    Unlike ``/history`` (raw states, ~10 days) these hourly rows are kept
    forever.  Returns ``{statistic_id: [row]}``; each row's ``start`` is a
    naive Warsaw-local datetime.
    """
    url = re.sub(r"^http", "ws", base_url.rstrip("/")) + "/websocket"
    with ws_connect(url, open_timeout=15, close_timeout=5, max_size=2**25) as ws:
        ws.recv(timeout=15)  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=15)).get("type") != "auth_ok":
            raise RuntimeError("HA websocket auth failed")
        ws.send(json.dumps({
            "id": 1,
            "type": "recorder/statistics_during_period",
            "start_time": start.astimezone(timezone.utc).isoformat(),
            "end_time": end.astimezone(timezone.utc).isoformat(),
            "statistic_ids": statistic_ids,
            "period": period,
            "types": list(types),
        }))
        msg = json.loads(ws.recv(timeout=60))
    if not msg.get("success"):
        raise RuntimeError(f"statistics_during_period failed: {msg.get('error')}")
    out: dict[str, list[dict]] = {}
    for sid, rows in (msg.get("result") or {}).items():
        for r in rows:
            st = r["start"]
            t = (
                datetime.fromtimestamp(st / 1000, timezone.utc)
                if isinstance(st, (int, float))
                else datetime.fromisoformat(st)
            )
            r["start"] = t.astimezone(WARSAW_TZ).replace(tzinfo=None)
        out[sid] = rows
    return out


def day_type(day: date, holidays: set[str]) -> int:
    """Weekday bucket for the load profile: a public holiday counts as Sunday."""
    return 6 if day.isoformat() in holidays else day.weekday()


def fetch_consumption_by_weekday(
    base_url: str, token: str, days: int = 28, holidays: set[str] = frozenset()
) -> dict:
    """Hourly load profile per weekday from the inverter output statistics.

    Each ``(weekday, hour)`` value blends two hourly means (energy, not a
    median of samples, so short heavy loads count):

      0.5 × the same weekday and hour over the past ``days`` (4 weeks = 4 samples)
    + 0.5 × the same hour over the last 7 days (the recent trend / season)

    Public holidays are bucketed as Sundays, here and in :func:`_build_net_load`.

    Returns:
        dict with:
          - profiles: ``{weekday(0..6): [24 kW]}``, Monday=0.
          - overall_hourly: ``[24 kW]`` — the last-7-days hourly mean (fallback).
          - overall_median: float — median of every hourly mean (last fallback).
    """
    now = datetime.now(WARSAW_TZ)
    try:
        rows = fetch_statistics(
            base_url, token, [INVERTER_OUTPUT_POWER_ENTITY], now - timedelta(days=days), now
        ).get(INVERTER_OUTPUT_POWER_ENTITY, [])
    except Exception:
        return _empty_consumption_profile()

    week_ago = now.replace(tzinfo=None) - timedelta(days=7)
    by_wd_hr: dict[tuple[int, int], list[float]] = defaultdict(list)
    recent: dict[int, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("mean") is None:
            continue
        t, kw = r["start"], r["mean"] * INVERTER_OUTPUT_POWER_TO_KW
        by_wd_hr[(day_type(t.date(), holidays), t.hour)].append(kw)
        by_hour[t.hour].append(kw)
        if t >= week_ago:
            recent[t.hour].append(kw)

    if not by_hour:
        return _empty_consumption_profile()

    overall_median = statistics.median(v for vs in by_hour.values() for v in vs)
    overall_hourly = [
        statistics.mean(recent[h] or by_hour[h]) if (recent[h] or by_hour[h]) else overall_median
        for h in range(24)
    ]
    profiles = {
        wd: [
            round(0.5 * statistics.mean(by_wd_hr[(wd, h)]) + 0.5 * overall_hourly[h], 3)
            if by_wd_hr[(wd, h)]
            else round(overall_hourly[h], 3)
            for h in range(24)
        ]
        for wd in range(7)
    }
    return {
        "profiles": profiles,
        "overall_hourly": [round(v, 3) for v in overall_hourly],
        "overall_median": round(overall_median, 3),
    }


def _empty_consumption_profile() -> dict:
    fallback = [1.5] * 24
    return {
        "profiles": {wd: list(fallback) for wd in range(7)},
        "overall_hourly": list(fallback),
        "overall_median": 1.5,
    }


# ── Reality fetchers (for prediction-vs-reality reconciliation) ────────────


def fetch_actual_consumption_for_date(
    base_url: str, token: str, target_date: date
) -> list[Optional[float]]:
    """Per-hour mean inverter output power (kW) on a given local date.

    From the long-term statistics — the same hourly means the profile is
    built from, so reconciliation compares like with like.  Returns a 24-slot
    list (``None`` where no data was recorded that hour).
    """
    start_local = datetime.combine(target_date, datetime.min.time(), WARSAW_TZ)
    try:
        rows = fetch_statistics(
            base_url, token, [INVERTER_OUTPUT_POWER_ENTITY], start_local, start_local + timedelta(days=1)
        ).get(INVERTER_OUTPUT_POWER_ENTITY, [])
    except Exception:
        return [None] * 24
    out: list[Optional[float]] = [None] * 24
    for r in rows:
        if r.get("mean") is not None and r["start"].date() == target_date:
            out[r["start"].hour] = round(r["mean"] * INVERTER_OUTPUT_POWER_TO_KW, 3)
    return out


def fetch_actual_solar_for_date(
    base_url: str, token: str, target_date: date
) -> list[Optional[float]]:
    """Per-hour measured PV yield (kWh) on ``target_date`` (Warsaw local).

    Steps of the inverter's lifetime PV counter (:data:`PV_ENERGY_TOTAL_ENTITY`),
    each booked to the local hour the counter moved in.  HA records only
    changes and the counter ticks every 0.01 kWh, so that is the hour the
    energy was made.  A drop below 90 % of the last reading is a counter reset
    (HA's ``total_increasing`` rule) and counts up from zero; a smaller dip is
    noise and is skipped.

    Returns 24 floats, or ``[None] * 24`` when the counter did not move that
    day (no data rather than a dark day).
    """
    start_local = datetime.combine(target_date, datetime.min.time(), WARSAW_TZ)
    end_local = start_local + timedelta(days=1)
    start_iso = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    url = (
        f"{base_url}/history/period/{start_iso}?"
        f"end_time={end_iso}&"
        f"filter_entity_id={PV_ENERGY_TOTAL_ENTITY}&minimal_response=true&no_attributes=true"
    )
    try:
        resp = requests.get(url, headers=_get_headers(base_url, token), timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return [None] * 24

    if not data or not isinstance(data, list) or not isinstance(data[0], list):
        return [None] * 24

    hourly_kwh = [0.0] * 24
    prev: Optional[float] = None
    for entry in data[0]:
        try:
            value = float(entry.get("state"))
            ts = datetime.fromisoformat(entry["last_changed"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue  # "unavailable" / "unknown"
        if prev is not None:
            if value >= prev:
                step = value - prev
            elif value < 0.9 * prev:  # counter reset
                step = value
            else:  # small dip: noise, keep the last good reading
                continue
            hourly_kwh[ts.astimezone(WARSAW_TZ).hour] += step
        prev = value

    if not any(hourly_kwh):
        return [None] * 24
    return [round(v, 3) for v in hourly_kwh]


def fetch_actuals_for_date(
    base_url: str,
    token: str,
    target_date: date,
) -> dict:
    """One-stop call: return per-hour actual consumption + solar for a date.

    Returns:
        {
          "consumption_kw":  [24 floats or None],
          "solar_kwh":       [24 floats or None],
          "net_load_kw":     [24 floats or None],  # max(0, cons - solar)
        }
    """
    cons = fetch_actual_consumption_for_date(base_url, token, target_date)
    sol = fetch_actual_solar_for_date(base_url, token, target_date)
    net: list[Optional[float]] = []
    for c, s in zip(cons, sol):
        if c is None:
            net.append(None)
        elif s is None:
            net.append(c)
        else:
            net.append(round(max(0.0, c - s), 3))
    return {"consumption_kw": cons, "solar_kwh": sol, "net_load_kw": net}


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

    Sources (attribute ``wh_period`` of each):
      - ``sensor.dom_energy_production_today``
      - ``sensor.dom_energy_production_tomorrow``
      - ``sensor.dom_energy_production_d2`` (day after tomorrow)

    Returns:
        dict with ``today``, ``tomorrow`` and ``d2`` keys, each a 24-element
        list of kWh per Warsaw local hour. Missing sensors yield a zero-filled day.
    """
    out = {"today": [0.0] * 24, "tomorrow": [0.0] * 24, "d2": [0.0] * 24}
    for key, entity in (
        ("today", SOLAR_FORECAST_TODAY_ENTITY),
        ("tomorrow", SOLAR_FORECAST_TOMORROW_ENTITY),
        ("d2", SOLAR_FORECAST_D2_ENTITY),
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


def pv_correction(
    forecast: list[float],
    actual: list[Optional[float]],
    soc_max: list[Optional[float]],
    now_hour: int,
) -> float:
    """Measured/forecast PV over today's finished hours, for the rest of today.

    Hours a full pack curtailed PV (BMS SOC ≥ :data:`PV_CURTAILED_SOC_PCT`)
    are skipped — the counter under-reads there.  Stays 1.0 (aim at the
    forecast) until the counted hours were forecast to make 0.5 kWh;
    clamped to 0.25–2.0 so one odd hour can't swing the plan.
    """
    f = a = 0.0
    for h in range(now_hour):
        if actual[h] is None or forecast[h] < 0.05 or (soc_max[h] or 0) >= PV_CURTAILED_SOC_PCT:
            continue
        f += forecast[h]
        a += actual[h]
    return 1.0 if f < 0.5 else round(min(2.0, max(0.25, a / f)), 3)


def fetch_pv_correction(base_url: str, token: str, forecast_today: list[float]) -> float:
    """:func:`pv_correction` from today's hourly PV yield and BMS SOC statistics."""
    now = datetime.now(WARSAW_TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        st = fetch_statistics(
            base_url, token, [PV_ENERGY_TODAY_ENTITY, BMS_SOC_ENTITY], midnight, now, ("change", "max")
        )
    except Exception:
        return 1.0
    actual: list[Optional[float]] = [None] * 24
    soc_max: list[Optional[float]] = [None] * 24
    for r in st.get(PV_ENERGY_TODAY_ENTITY, []):
        if r.get("change") is not None:
            actual[r["start"].hour] = max(0.0, r["change"])
    for r in st.get(BMS_SOC_ENTITY, []):
        soc_max[r["start"].hour] = r.get("max")
    return pv_correction(forecast_today, actual, soc_max, now.hour)


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
    n_blocks = max(1, -(-len(prices) // 24))  # a padded tail may end mid-day

    if horizon == "today":
        return [today]
    if horizon == "tomorrow":
        return [today + timedelta(days=1)]
    # "available" — first block is today, second (if present) is tomorrow,
    # extra blocks (rare) chain forward.
    return [today + timedelta(days=i) for i in range(n_blocks)]


def _pad_with_estimates(prices: list[dict], n: int) -> list[dict]:
    """Pad ``prices`` (whole days) to ``n`` hours by repeating the last known
    day hour by hour, flagged ``is_estimated``."""
    last = prices[-24:]
    return prices + [
        {**last[i % 24], "is_estimated": True, "is_cheap": False, "is_expensive": False}
        for i in range(len(prices), n)
    ]


def _build_net_load(
    prices: list[dict],
    consumption: dict,
    solar: dict,
    horizon_dates: list[date],
    overrides: Optional[OptimizerOverrides] = None,
    holidays: set[str] = frozenset(),
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Assemble per-horizon-hour (net_load, raw_consumption, solar, pv_surplus).

    Net load = max(0, consumption[weekday][hour] - solar[date][hour]).

    When an :class:`OptimizerOverrides` is supplied, the following knobs are
    applied IN ORDER so layering is predictable:

      1. ``consumption_profile``  → replace the auto-learned weekday map.
      2. ``consumption_scale``    → multiplier on the per-hour kW.
      3. ``solar_override_kwh``   → full PV forecast replacement.
      4. ``solar_scale``          → multiplier on the PV forecast.
      5. (net = max(0, consumption − solar))
      6. ``extra_loads``          → add planned one-shot loads (kW × duration_h);
                                     they eat PV surplus first, then net load.
      7. ``load_override_kw``     → final hard replacement of the net load.

    ``pv_surplus`` = max(0, solar − consumption): what PV has left over for
    the battery after the house.

    Returns four parallel lists, each ``len(prices)`` long.
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
    surplus: list[float] = []
    today = datetime.now(WARSAW_TZ).date()

    for i, _ in enumerate(prices):
        block_idx = i // 24
        hour = i % 24
        day = (
            horizon_dates[block_idx]
            if block_idx < len(horizon_dates)
            else horizon_dates[-1]
        )

        wd = day_type(day, holidays)
        cons_kw = profiles.get(wd, overall_hourly)[hour]
        # (2) Consumption scale.
        cons_kw *= overrides.consumption_scale
        # (2b) Learned per-(weekday, hour) consumption multiplier from
        # reconciliation history.  1.0 (no-op) when nothing has been
        # observed for that bucket yet, or when use_adjustments=false at the
        # REST layer (in which case the field is None).
        cons_kw *= overrides.consumption_adjustment_for(wd, hour)

        # Pick the solar forecast by calendar date, not block index —
        # horizon='tomorrow' has tomorrow in block 0.
        key = {0: "today", 1: "tomorrow", 2: "d2"}.get((day - today).days)
        sol_kwh = solar.get(key, [0.0] * 24)[hour] if key else 0.0

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
        surplus.append(round(max(0.0, sol_kwh - cons_kw), 3))

    # (6) Extra one-shot loads (additive).
    for load in overrides.extra_loads:
        day_idx = load.get("day", 0)
        base = day_idx * 24
        for offset in range(load["duration_h"]):
            idx = base + load["hour"] + offset
            if 0 <= idx < len(net):
                from_pv = min(surplus[idx], load["kw"])
                surplus[idx] = round(surplus[idx] - from_pv, 3)
                net[idx] = round(net[idx] + load["kw"] - from_pv, 3)

    # (7) Final hard load override (replaces net entirely, length-tolerant).
    if overrides.load_override_kw is not None:
        for i in range(min(len(net), len(overrides.load_override_kw))):
            net[i] = round(max(0.0, overrides.load_override_kw[i]), 3)

    return net, raw, sol, surplus


# ── One-shot fetcher used by main.py / rest_service.py ─────────────────────


def fetch_all_data(
    base_url: str,
    token: str,
    days: int = 28,
    horizon: str = "available",
    overrides: Optional[OptimizerOverrides] = None,
) -> dict:
    """Fetch everything the optimizer needs in one call.

    Args:
        base_url: HA API base URL (e.g. https://ha.kompfix.pl/api).
        token: Long-lived access token.
        days: Days of history for the weekday × hour consumption profile
              (default 28 — four samples of every weekday).
        horizon: 'today', 'tomorrow', or 'available'.  'available' is padded
                 with estimated prices to :data:`MIN_HORIZON_H` hours ahead.
        overrides: Optional :class:`OptimizerOverrides` applied to the
                   consumption profile, PV forecast, and net load.  When
                   omitted, default (no-op) overrides are used so callers
                   that don't care keep working unchanged.

    Returns:
        dict with:
          - prices: list of hourly price dicts (24, 48, or padded with
                    ``is_estimated`` hours).
          - pv_correction: measured/forecast PV ratio applied to the rest of today.
          - soc: starting SOC fraction (0–1).
          - soc_source: 'bms', 'voltage' (BMS silent) or 'default' (0.5).
          - dummy_loads: net load per horizon hour (consumption − solar, ≥ 0).
                         Length matches ``prices``.
          - raw_consumption: gross consumption per horizon hour (weekday-keyed).
          - solar_forecast_kwh: per-horizon-hour solar production (kWh).
          - pv_surplus_kw: per-horizon-hour PV left over after the house
                           (≥ 0) — free battery charging.
          - consumption_profiles: full ``{weekday: [24 kW]}`` map.
          - out_of_work_days: list of out-of-work dates (Sat/Sun + PL holidays)
                              covering the horizon.
          - horizon_dates: dates assigned to each 24-hour block.
    """
    if overrides is None:
        overrides = OptimizerOverrides()

    now = datetime.now(WARSAW_TZ)
    prices = fetch_hourly_prices(base_url, token, horizon=horizon)
    if horizon == "available" and prices:
        prices = _pad_with_estimates(prices, now.hour + MIN_HORIZON_H)
    soc, soc_source = fetch_initial_soc(base_url, token)

    h_dates = _horizon_dates(horizon, prices)

    # Holiday set for every year touched by the horizon and the profile history.
    holidays: set[str] = set()
    for year in {d.year for d in h_dates} | {(now - timedelta(days=days)).year}:
        holidays |= fetch_holidays_pl(year)

    consumption = fetch_consumption_by_weekday(base_url, token, days=days, holidays=holidays)
    solar = fetch_solar_forecast(base_url, token)
    # Aim at the forecast; only the rest of today follows what PV really did so far.
    pv_corr = fetch_pv_correction(base_url, token, solar["today"])
    solar["today"] = [v * pv_corr if h >= now.hour else v for h, v in enumerate(solar["today"])]

    out_of_work_days = [
        d.isoformat() for d in h_dates if is_out_of_work(d, holidays)
    ]

    net_load, raw_consumption, solar_per_hour, pv_surplus = _build_net_load(
        prices, consumption, solar, h_dates, overrides=overrides, holidays=holidays
    )

    return {
        "prices": prices,
        "soc": soc,
        "soc_source": soc_source,
        "dummy_loads": net_load,
        "raw_consumption": raw_consumption,
        "solar_forecast_kwh": solar_per_hour,
        "pv_surplus_kw": pv_surplus,
        "pv_correction": pv_corr,
        "consumption_profiles": consumption["profiles"],
        "out_of_work_days": out_of_work_days,
        "horizon_dates": [d.isoformat() for d in h_dates],
    }
