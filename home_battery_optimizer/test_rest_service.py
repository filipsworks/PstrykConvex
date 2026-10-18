#!/usr/bin/env python3
"""Self-check for /optimize: one joint plan over today, tomorrow and the
estimated tail, split per day, with the ``plan`` block the HA EMS reads.

Runs offline: ``fetch_all_data`` is replaced by a fixed two-day horizon.

Run:
    .venv/bin/python home_battery_optimizer/test_rest_service.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import api  # noqa: E402
import rest_service as rs  # noqa: E402

TODAY = datetime.now(rs.WARSAW_TZ).date()
DAY = [1.1, 1.0, 0.9, 0.9, 1.0, 1.1, 1.5, 1.9, 1.8, 1.3, 1.0, 0.8,
       0.7, 0.7, 0.8, 1.2, 1.6, 2.0, 2.1, 2.0, 1.8, 1.5, 1.2, 1.1]
LOAD = [0.3] * 6 + [0.6] * 3 + [0.8] * 8 + [0.6] * 7


def fake_fetch_all_data(base_url, token, days, horizon, overrides):
    prices = [{"hour": h, "price": p} for h, p in enumerate(DAY)] * 2
    prices = api._pad_with_estimates(prices, 60)
    return {
        "prices": prices, "soc": 0.4, "soc_source": "bms",
        "dummy_loads": LOAD * 2 + LOAD[:12], "raw_consumption": LOAD * 2 + LOAD[:12],
        "solar_forecast_kwh": [0.0] * 60, "pv_surplus_kw": [0.0] * 60, "pv_correction": 1.0,
        "out_of_work_days": [],
        "horizon_dates": [(TODAY + timedelta(days=i)).isoformat() for i in range(3)],
    }


def main() -> None:
    rs.fetch_all_data = fake_fetch_all_data
    rs.history_db.record_predictions = lambda rows: None  # keep the real DB clean
    c = rs.app.test_client()

    r = c.get("/optimize?ha_token=x&target_soc=60").get_json()
    days, plan = r["days"], r["plan"]
    assert [d["date"] for d in days] == [(TODAY + timedelta(days=i)).isoformat() for i in range(3)]
    assert days[2].get("is_estimated") and len(days[2]["decisions"]) == 12
    # The 60 % pin applies to days with published prices only.
    assert days[0]["effective_target_soc_pct"] == days[1]["effective_target_soc_pct"] == 60
    assert "effective_target_soc_pct" not in days[2]
    assert days[1]["summary"]["target_reached"] is True and days[1]["summary"]["final_soc"] >= 59.9

    hours = [d for day in days for d in day["decisions"]]
    starts = [datetime.fromisoformat(d["start"]) for d in hours]
    assert all(b - a == timedelta(hours=1) for a, b in zip(starts, starts[1:]))
    assert all(d["mode"] in ("offgrid", "charge") and d["ac_on"] == (d["mode"] == "charge") for d in hours)
    assert plan["now"]["start"] == hours[0]["start"] and plan["target_soc"] == 60
    # Every charging hour sits in exactly one AC window.
    assert sum(len([d for d in hours if w["start"] <= d["start"] < w["end"]]) for w in plan["ac_windows"]) \
        == plan["totals"]["ac_hours"] == len([d for d in hours if d["ac_on"]])
    assert plan["totals"]["estimated_prices_from"] == days[2]["decisions"][0]["start"]

    # 'today' / 'tomorrow' are days of the same joint plan: tomorrow starts
    # where today ends, not from the SOC right now.
    full = c.get("/optimize?ha_token=x").get_json()["days"]
    today = c.get("/optimize?ha_token=x&horizon=today").get_json()
    tomorrow = c.get("/optimize?ha_token=x&horizon=tomorrow").get_json()
    assert (today["date"], tomorrow["date"]) == (full[0]["date"], full[1]["date"])
    assert today["decisions"] == full[0]["decisions"] and tomorrow["decisions"] == full[1]["decisions"]
    first = tomorrow["decisions"][0]
    soc_before = first["soc_pct"] - (first["charge_wh"] + first["pv_charge_wh"] - first["discharge_wh"]) / 166.4
    assert abs(soc_before - today["summary"]["final_soc"]) < 0.2, (soc_before, today["summary"]["final_soc"])
    sens = c.get("/sensitivity?ha_token=x&horizon=tomorrow").get_json()
    assert sens["date"] == full[1]["date"] and sens["initial_soc_pct"] == today["summary"]["final_soc"]

    # Automatic target: never below MinSOC, no day pins.
    r = c.get("/optimize?ha_token=x&target_soc=-1").get_json()
    assert r["plan"]["target_soc"] == "auto"
    assert r["plan"]["totals"]["min_soc_pct"] >= 10 - 0.1
    assert all("effective_target_soc_pct" not in d for d in r["days"])
    print("rest_service self-check OK")


if __name__ == "__main__":
    main()
