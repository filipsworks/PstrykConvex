#!/usr/bin/env python3
"""Unit-scale checks for the DessMonitor changeover in api.py.

The new inverter sensor reports output power in **W** while the retired
"Solar of Things" sensor reported **kW**.  Both feed the same weekday
consumption profile, so a per-entity scale factor is the one thing that
must not regress.  Runs offline against a stubbed ``requests.get``.

Run:
    .venv/bin/python home_battery_optimizer/test_api_units.py
"""

from __future__ import annotations

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


def main() -> None:
    # 2000 W on the new sensor and 2.0 kW on the legacy one are the same load;
    # after scaling every bucket must read 2.0 kW.
    payload = [
        _entries(api.INVERTER_OUTPUT_POWER_ENTITY, 2000, [1, 2, 3]),
        _entries("sensor.gniazdo_output_active_power", 2.0, [25, 26, 27]),
    ]
    api.requests.get = lambda *a, **k: _Resp(payload)  # type: ignore[assignment]

    prof = api.fetch_consumption_by_weekday("http://x/api", "tok", days=7)
    assert prof["overall_median"] == 2.0, prof["overall_median"]
    assert all(v == 2.0 for v in prof["overall_hourly"]), prof["overall_hourly"]
    print("  ✓ W and kW sources both normalise to 2.0 kW")

    # Same for the reconciliation actuals fetcher (new sensor only).
    today = datetime.now(api.WARSAW_TZ).date()
    ts = datetime.now(api.WARSAW_TZ).replace(hour=12, minute=0).isoformat()
    api.requests.get = lambda *a, **k: _Resp(  # type: ignore[assignment]
        [[{"entity_id": api.INVERTER_OUTPUT_POWER_ENTITY, "state": "1500", "last_changed": ts}]]
    )
    actual = api.fetch_actual_consumption_for_date("http://x/api", "tok", today)
    assert actual[12] == 1.5, actual[12]
    assert actual[0] is None
    print("  ✓ actuals fetcher returns kW (1500 W → 1.5 kW)")

    # Unknown entity must not be silently rescaled by another sensor's factor.
    assert dict(api.CONSUMPTION_SOURCES)[api.INVERTER_OUTPUT_POWER_ENTITY] == 0.001

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
