#!/usr/bin/env python3
"""End-to-end smoke test for the prediction-vs-reality plumbing.

Runs entirely against a temp SQLite DB and the Flask test client so we
don't need Home Assistant to be reachable.  Covers:

  1.  /optimize records per-hour predictions in the history DB.
  2.  /reconcile pulls injected "actuals" (via a mocked fetcher) and
       updates the learned multipliers in the right direction.
  3.  /report shows the side-by-side prediction-vs-reality table with
       sensible MAPE numbers.
  4.  /optimize?use_adjustments=false gives a different net-load than the
       adjusted run — proof that the multipliers are actually fed in.
  5.  /sensitivity?use_adjustments=false works the same way.
  6.  /adjustments returns a 7×24 grid; DELETE resets it.

Run:
    .venv/bin/python home_battery_optimizer/test_reconciliation.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path


# Make the package importable when invoked from the repo root.
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))


def _set_temp_db() -> Path:
    fd, path = tempfile.mkstemp(prefix="battery_history_test_", suffix=".db")
    os.close(fd)
    os.environ["BATTERY_HISTORY_DB"] = path
    return Path(path)


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _section(title: str) -> None:
    print(f"\n── {title} ──")


def main() -> None:
    db_path = _set_temp_db()
    print(f"Temp DB: {db_path}")

    # Late import so BATTERY_HISTORY_DB is in effect before history_db opens.
    import history_db  # noqa: E402
    import reconciliation  # noqa: E402
    import rest_service  # noqa: E402

    client = rest_service.app.test_client()

    # ── 1. /optimize records predictions ──────────────────────────────
    _section("Recording predictions via /optimize?mock=true")
    resp = client.get("/optimize?mock=true&horizon=today")
    if resp.status_code != 200:
        _fail(f"/optimize returned {resp.status_code}: {resp.data}")
    payload = resp.get_json()
    target_date = payload.get("date")
    if not target_date:
        _fail("response missing 'date' field")
    _ok(f"optimize OK for {target_date}, {len(payload['decisions'])} decisions")

    preds = history_db.latest_predictions_for_date(target_date)
    if len(preds) != 24:
        _fail(f"expected 24 recorded predictions, got {len(preds)}")
    _ok(f"recorded {len(preds)} predictions in history DB")

    # ── 2. Inject mocked actuals where consumption is ~30% higher ─────
    _section("Injecting synthetic actuals (consumption +30%, solar 0)")

    def fake_fetcher(base_url: str, token: str, day: date) -> dict:
        # 30% higher consumption than the predictions, no solar.
        cons = []
        for h in range(24):
            pred = next((p for p in preds if p["hour"] == h), None)
            base = pred["predicted_consumption_kw"] if pred else 1.0
            cons.append(round(base * 1.30, 3))
        sol = [0.0] * 24
        net = [round(c, 3) for c in cons]
        return {"consumption_kw": cons, "solar_kwh": sol, "net_load_kw": net}

    day_obj = date.fromisoformat(target_date)
    reconciliation.pull_actuals_from_ha(
        base_url="http://mock",
        token="mock",
        target_date=day_obj,
        fetcher=fake_fetcher,
    )
    actuals = history_db.actuals_for_date(target_date)
    if len(actuals) != 24:
        _fail(f"expected 24 actuals, got {len(actuals)}")
    _ok(f"stored {len(actuals)} hourly actuals")

    # ── 3. Reconcile → multipliers should shift toward ~1.3 ───────────
    _section("Reconciling prediction vs reality")
    result = reconciliation.reconcile_date(target_date, alpha=0.5)
    if result.hours_reconciled == 0:
        _fail("reconcile_date saw no overlap")
    _ok(
        f"hours_reconciled={result.hours_reconciled}, "
        f"cons MAPE={result.consumption_mape}%, "
        f"cons bias={result.consumption_bias}"
    )
    if result.consumption_bias is None or result.consumption_bias < 0.1:
        _fail(
            f"expected consumption bias near +0.3, got {result.consumption_bias}"
        )
    _ok("consumption bias is positive (actuals exceeded predictions)")

    adj = history_db.get_adjustments()
    if not adj:
        _fail("no adjustments stored after reconciliation")
    sample_mult = next(iter(adj.values()))["consumption_multiplier"]
    if sample_mult <= 1.0:
        _fail(f"expected consumption multiplier >1.0, got {sample_mult}")
    _ok(f"adjustments populated, sample consumption_multiplier={sample_mult}")

    # ── 4. /optimize WITH adjustments vs WITHOUT ──────────────────────
    _section("With vs without adjustments")
    with_adj = client.get("/optimize?mock=true&horizon=today").get_json()
    without_adj = client.get(
        "/optimize?mock=true&horizon=today&use_adjustments=false"
    ).get_json()

    with_loads = with_adj["raw_consumption_kw"]
    without_loads = without_adj["raw_consumption_kw"]
    # 'raw_consumption_kw' in the mock path mirrors MOCK_DUMMY_LOADS (the
    # pre-adjustment baseline) so the visible "raw" is identical.  The
    # adjustment shows up in `total_active_kw` of each decision because the
    # mock-mode override path scales the net-load *before* the optimizer
    # sees it.
    with_kw_h0 = with_adj["decisions"][0]["total_active_kw"]
    without_kw_h0 = without_adj["decisions"][0]["total_active_kw"]
    if abs(with_kw_h0 - without_kw_h0) < 1e-6:
        _fail(
            f"expected different total_active_kw with vs without adjustments "
            f"(with={with_kw_h0}, without={without_kw_h0})"
        )
    _ok(
        f"adjusted run differs from raw: hour 0 total_active_kw "
        f"with={with_kw_h0} vs without={without_kw_h0}"
    )

    if with_adj["adjustments"]["applied"] is not True:
        _fail("with-adjustments response missing applied=true flag")
    if without_adj["adjustments"]["applied"] is not False:
        _fail("without-adjustments response missing applied=false flag")
    _ok("adjustments metadata reported correctly in both responses")

    # ── 5. /sensitivity respects use_adjustments ──────────────────────
    _section("/sensitivity respects use_adjustments")
    sens_with = client.get("/sensitivity?mock=true").get_json()
    sens_without = client.get(
        "/sensitivity?mock=true&use_adjustments=false"
    ).get_json()
    if sens_with["adjustments"]["applied"] is not True:
        _fail("sensitivity with adjustments did not advertise applied=true")
    if sens_without["adjustments"]["applied"] is not False:
        _fail("sensitivity without adjustments did not advertise applied=false")
    _ok(
        f"sensitivity baselines: "
        f"with_adj={sens_with['baseline']['cost_pln']:.3f} PLN, "
        f"without_adj={sens_without['baseline']['cost_pln']:.3f} PLN"
    )

    # ── 6. /report endpoint ───────────────────────────────────────────
    _section("/report endpoint")
    rep = client.get(f"/report?date={target_date}").get_json()
    if rep["summary"]["hours_compared"] != 24:
        _fail(f"report.hours_compared != 24 (got {rep['summary']['hours_compared']})")
    if rep["summary"]["consumption_mape"] is None:
        _fail("report.consumption_mape missing")
    _ok(
        f"report MAPE: cons={rep['summary']['consumption_mape']}%, "
        f"net={rep['summary']['net_load_mape']}%, "
        f"cost_delta={rep['summary']['cost_delta_pln']} PLN"
    )

    # ── 7. /adjustments grid + DELETE ─────────────────────────────────
    _section("/adjustments grid + reset")
    grid = client.get("/adjustments").get_json()
    if len(grid["grid"]) != 7 * 24:
        _fail(f"adjustments grid should be 168 cells, got {len(grid['grid'])}")
    if grid["buckets"] == 0:
        _fail("adjustments grid reports 0 populated buckets after reconciliation")
    _ok(f"grid: 168 cells, {grid['buckets']} populated")

    del_resp = client.delete("/adjustments")
    if del_resp.get_json().get("reset") is not True:
        _fail("DELETE /adjustments did not return reset=true")
    grid_after = client.get("/adjustments").get_json()
    if grid_after["buckets"] != 0:
        _fail("adjustments not actually cleared after DELETE")
    _ok("DELETE /adjustments clears the table")

    # ── 8. /reconciliation_log ────────────────────────────────────────
    _section("/reconciliation_log")
    log = client.get("/reconciliation_log").get_json()
    if not log["log"]:
        _fail("reconciliation_log empty after a reconcile")
    if target_date not in log["reconciled_dates"]:
        _fail(f"reconciled_dates missing {target_date}")
    _ok(f"log has {len(log['log'])} entries, target date present")

    # ── 9. Re-run reconcile via /reconcile?mock=true ──────────────────
    _section("/reconcile?mock=true (rerun with stored actuals only)")
    rec = client.get(f"/reconcile?date={target_date}&mock=true&alpha=0.5").get_json()
    if not rec["results"]:
        _fail("/reconcile?mock=true returned no results")
    first = rec["results"][0]
    if first["hours_reconciled"] != 24:
        _fail(f"rerun reconciled {first['hours_reconciled']} hours, expected 24")
    _ok(
        f"rerun OK: {first['hours_reconciled']} hours, "
        f"cons_mape={first['consumption_mape']}%, bias={first['consumption_bias']}"
    )

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
