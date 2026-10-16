#!/usr/bin/env python3
"""Unit-scale, profile, PV and SOC-source checks for api.py.

The inverter reports output power in **W** while the optimizer works in
**kW**, so the scale factor is the one thing that must not regress.  Runs
offline against a stubbed ``requests.get`` and a fake HA WebSocket.

Run:
    .venv/bin/python home_battery_optimizer/test_api_units.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import api  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _entries(entity_id, value, hours_ago_list):
    """History sub-list: only the first entry carries entity_id (minimal_response)."""
    out = []
    for i, h in enumerate(hours_ago_list):
        ts = (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()
        entry = {"state": str(value), "last_changed": ts}
        if i == 0:
            entry["entity_id"] = entity_id
        out.append(entry)
    return out


class _FakeWS:
    """HA WebSocket: auth handshake, then one statistics result."""

    def __init__(self, result):
        self.inbox = [{"type": "auth_required"}, {"type": "auth_ok"},
                      {"id": 1, "type": "result", "success": True, "result": result}]
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def recv(self, timeout=None):
        return json.dumps(self.inbox.pop(0))

    def send(self, msg):
        self.sent.append(json.loads(msg))


def _stats(rows_by_id):
    """Install a fake WebSocket answering with ``{id: [(local datetime, {type: value})]}``."""
    ws = _FakeWS({
        sid: [{"start": dt.replace(tzinfo=api.WARSAW_TZ).timestamp() * 1000, **vals} for dt, vals in rows]
        for sid, rows in rows_by_id.items()
    })
    api.ws_connect = lambda url, **k: ws  # type: ignore[assignment]
    return ws


def main() -> None:
    today = datetime.now(api.WARSAW_TZ).date()
    out_id = api.INVERTER_OUTPUT_POWER_ENTITY

    # The inverter reports W; every profile bucket must come out in kW.
    ws = _stats({out_id: [(datetime.combine(today - timedelta(days=d), datetime.min.time()) + timedelta(hours=h),
                           {"mean": 2000}) for d in range(1, 28) for h in range(24)]})
    prof = api.fetch_consumption_by_weekday("https://x/api", "tok", days=28)
    assert ws.sent[0] == {"type": "auth", "access_token": "tok"}
    assert ws.sent[1]["type"] == "recorder/statistics_during_period" and ws.sent[1]["period"] == "hour"
    assert prof["overall_median"] == 2.0, prof["overall_median"]
    assert all(v == 2.0 for wd in range(7) for v in prof["profiles"][wd]), prof["profiles"]
    print("  ✓ statistics over WebSocket, inverter W normalises to 2.0 kW")

    # Blend: 0.5 × same weekday (4 weeks) + 0.5 × same hour (last 7 days);
    # a public holiday counts as a Sunday.
    holiday = today - timedelta(days=14)
    rows = [(datetime.combine(today - timedelta(days=d), datetime.min.time()).replace(hour=10),
             {"mean": 5000 if d == 14 else 1000}) for d in range(1, 28)]
    _stats({out_id: rows})
    prof = api.fetch_consumption_by_weekday("https://x/api", "tok", days=28, holidays={holiday.isoformat()})
    sundays = [d for d in range(1, 28) if (today - timedelta(days=d)).weekday() == 6 and d != 14]
    expect_sun = 0.5 * (len(sundays) * 1.0 + 5.0) / (len(sundays) + 1) + 0.5 * 1.0
    assert abs(prof["profiles"][6][10] - expect_sun) < 1e-3, (prof["profiles"][6][10], expect_sun)
    if holiday.weekday() != 6:
        assert prof["profiles"][holiday.weekday()][10] == 1.0, prof["profiles"][holiday.weekday()]
    assert api.day_type(holiday, {holiday.isoformat()}) == 6
    print("  ✓ profile blends weekday (4 wk) with last 7 days; holidays → Sunday")

    # Reconciliation actuals use the same hourly means.
    _stats({out_id: [(datetime.combine(today, datetime.min.time()).replace(hour=12), {"mean": 1500})]})
    actual = api.fetch_actual_consumption_for_date("https://x/api", "tok", today)
    assert actual[12] == 1.5, actual[12]
    assert actual[0] is None
    print("  ✓ actuals fetcher returns kW (1500 W → 1.5 kW)")

    # Intraday PV correction: measured/forecast over finished hours, skipping
    # hours a full pack curtailed; 1.0 until there is enough forecast to judge.
    fc = [0.0] * 8 + [1.0, 1.0, 1.0, 1.0] + [0.0] * 12
    act = [None] * 24
    act[8], act[9], act[10] = 0.5, 0.5, 0.2
    soc = [None] * 24
    soc[10] = 99
    assert api.pv_correction(fc, act, soc, now_hour=11) == 0.5
    assert api.pv_correction(fc, act, soc, now_hour=8) == 1.0  # nothing finished yet
    act[8] = act[9] = 10.0
    assert api.pv_correction(fc, act, soc, now_hour=11) == 2.0  # clamped
    assert api.pv_correction(fc, [None] * 24, soc, now_hour=11) == 1.0
    print("  ✓ PV correction: ratio of finished hours, curtailed hours skipped, clamped")

    # Estimated tail: the last known day repeated hour by hour, flagged.
    known = [{"hour": h, "price": float(h), "is_cheap": h == 3, "is_expensive": False} for h in range(24)]
    padded = api._pad_with_estimates(known, 30)
    assert len(padded) == 30 and not any(p.get("is_estimated") for p in padded[:24])
    assert all(p["is_estimated"] and p["hour"] == i % 24 and p["price"] == float(i % 24)
               for i, p in enumerate(padded[24:], start=24))
    assert api._horizon_dates("available", padded) == [today, today + timedelta(days=1)]
    print("  ✓ prices padded with an estimated tail; partial day keeps its date")

    # Measured PV = steps of the lifetime counter, booked to the hour they land
    # in; "unavailable" skipped, a small dip ignored, a big drop is a reset.
    def at(h, m=0):
        midnight = datetime.combine(today, datetime.min.time(), api.WARSAW_TZ)
        return midnight.replace(hour=h, minute=m).isoformat()

    readings = [("404.49", 0, 0), ("unavailable", 0, 14), ("404.50", 7, 32),
                ("404.95", 8, 50), ("404.90", 9, 5), ("405.71", 9, 29), ("0.30", 12, 0)]
    api.requests.get = lambda *a, **k: _Resp(  # type: ignore[assignment]
        [[{"state": s, "last_changed": at(h, m)} for s, h, m in readings]]
    )
    sol = api.fetch_actual_solar_for_date("http://x/api", "tok", today)
    assert (sol[0], sol[7], sol[8], sol[9], sol[12]) == (0.0, 0.01, 0.45, 0.76, 0.3), sol
    api.requests.get = lambda *a, **k: _Resp(  # type: ignore[assignment]
        [[{"state": "404.49", "last_changed": at(0)}]]
    )
    assert api.fetch_actual_solar_for_date("http://x/api", "tok", today) == [None] * 24
    print("  ✓ PV actuals from counter steps (dip skipped, reset counted, flat day = None)")

    # SOC source chain: BMS (incl. last reading during a dropout) → voltage → 0.5.
    def stub(bms_states, voltage_state):
        def get(url, *a, **k):
            if api.BMS_SOC_ENTITY in url:
                return _Resp([[{"state": st} for st in bms_states]] if bms_states else [])
            return _Resp({"state": voltage_state})
        api.requests.get = get  # type: ignore[assignment]

    stub(["19", "20"], "53.2")
    assert api.fetch_initial_soc("http://x/api", "tok") == (0.2, "bms")
    stub(["20", "unavailable"], "53.2")  # BLE dropout: keep last BMS reading
    assert api.fetch_initial_soc("http://x/api", "tok") == (0.2, "bms")
    stub(["unavailable"], "52.0")  # BMS silent past the window
    soc, src = api.fetch_initial_soc("http://x/api", "tok")
    assert src == "voltage" and abs(soc - 0.5) < 1e-9, (soc, src)
    stub(None, "unavailable")
    assert api.fetch_initial_soc("http://x/api", "tok") == (0.5, "default")
    print("  ✓ SOC chain: bms → last bms in window → voltage → default")

    # Net load / PV surplus split, with the solar forecast picked by date:
    # a 'tomorrow' horizon has tomorrow in block 0 and must use tomorrow's PV.
    flat = [1.0] * 24
    consumption = {"profiles": {wd: flat for wd in range(7)}, "overall_hourly": flat}
    solar = {"today": [0.0] * 24, "tomorrow": [3.0 if 10 <= h < 14 else 0.0 for h in range(24)]}
    prices = [{"hour": h, "price": 1.0} for h in range(24)]
    tomorrow = today + timedelta(days=1)
    overrides = api.OptimizerOverrides(extra_loads=[{"hour": 10, "kw": 0.5, "duration_h": 1}])
    net, _, _, surplus = api._build_net_load(prices, consumption, solar, [tomorrow], overrides)
    assert net[12] == 0.0 and surplus[12] == 2.0, (net[12], surplus[12])
    assert net[9] == 1.0 and surplus[9] == 0.0
    assert net[10] == 0.0 and surplus[10] == 1.5  # extra load eats PV surplus first
    solar["d2"] = [0.5] * 24
    net, _, _, _ = api._build_net_load(prices, consumption, solar, [today + timedelta(days=2)])
    assert net[0] == 0.5, net[0]
    print("  ✓ PV surplus split out and taken from the matching day's forecast (incl. D+2)")

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
