"""Compare predictions vs reality and roll the learned multipliers forward.

The unit of learning is the ``(weekday, hour)`` bucket — same shape as the
consumption profile the optimizer feeds on.  For each hour with both a
prediction and an actual we compute ``ratio = actual / predicted``, then
exponentially smooth that into the stored multiplier:

    new_mult = alpha * ratio + (1 - alpha) * old_mult

Ratios are clamped to a sane range (default 0.5 – 2.0) so a one-off spike
doesn't poison the multiplier; near-zero predictions are skipped to avoid
explosive divisions.

The same mechanism applies to solar — if the predicted PV was 4 kWh and we
actually got 3, the solar multiplier nudges toward 0.75 so future runs
plan with the corrected forecast.

After reconciliation, ``adjustments`` rows exist for every bucket we ever
had a sample for (1.0 means "no correction needed"); the optimizer reads
them on every run unless the caller passes ``use_adjustments=false``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import history_db


# Tunables — kept module-level so a hot-reload changes them on the fly.
DEFAULT_ALPHA = 0.25            # learning rate (higher = faster adapt, more jitter)
DEFAULT_MIN_RATIO = 0.5          # clamp floor on per-sample ratios
DEFAULT_MAX_RATIO = 2.0          # clamp ceiling
DEFAULT_MIN_KW = 0.05            # treat values below this as noise (skip)
DEFAULT_MIN_KWH_SOLAR = 0.05    # same for solar


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class ReconcileResult:
    target_date: str
    hours_reconciled: int
    consumption_mape: Optional[float]
    solar_mape: Optional[float]
    consumption_bias: Optional[float]   # mean(actual/predicted) − 1, signed
    solar_bias: Optional[float]
    per_hour: list[dict]                # raw deltas per hour
    updated_buckets: list[tuple[int, int]]
    notes: str = ""


# ── Core reconciliation ──────────────────────────────────────────────────


def reconcile_date(
    target_date: str,
    db_path=None,
    alpha: float = DEFAULT_ALPHA,
    min_ratio: float = DEFAULT_MIN_RATIO,
    max_ratio: float = DEFAULT_MAX_RATIO,
    min_kw: float = DEFAULT_MIN_KW,
    min_kwh_solar: float = DEFAULT_MIN_KWH_SOLAR,
) -> ReconcileResult:
    """Compare stored predictions vs stored actuals for ``target_date`` and
    update the per-(weekday, hour) multipliers in place.

    Returns a :class:`ReconcileResult` summarising the run.  Also writes a
    row to ``reconciliation_log`` for the dashboard.

    Caller is responsible for ensuring ``actuals`` exist for ``target_date``
    (e.g. via :func:`pull_actuals_from_ha`).  If no overlap exists, the
    return value reports 0 hours reconciled and no multipliers change.
    """
    preds = history_db.latest_predictions_for_date(target_date, db_path=db_path)
    actuals = history_db.actuals_for_date(target_date, db_path=db_path)

    if not preds or not actuals:
        notes = "no predictions" if not preds else "no actuals"
        history_db.log_reconciliation(
            target_date, 0, None, None, None, None, notes=notes, db_path=db_path
        )
        return ReconcileResult(
            target_date=target_date,
            hours_reconciled=0,
            consumption_mape=None,
            solar_mape=None,
            consumption_bias=None,
            solar_bias=None,
            per_hour=[],
            updated_buckets=[],
            notes=notes,
        )

    pred_by_hour = {p["hour"]: p for p in preds}
    act_by_hour = {a["hour"]: a for a in actuals}

    existing = history_db.get_adjustments(db_path=db_path)
    updated_buckets: list[tuple[int, int]] = []
    per_hour: list[dict] = []

    cons_abs_pct_errs: list[float] = []
    sol_abs_pct_errs: list[float] = []
    cons_ratios: list[float] = []
    sol_ratios: list[float] = []

    overlap_hours = sorted(set(pred_by_hour) & set(act_by_hour))

    for h in overlap_hours:
        p = pred_by_hour[h]
        a = act_by_hour[h]
        weekday = int(p["weekday"])

        old = existing.get(
            (weekday, h),
            {
                "consumption_multiplier": 1.0,
                "solar_multiplier": 1.0,
                "sample_count": 0,
            },
        )
        new_cons_mult = old["consumption_multiplier"]
        new_sol_mult = old["solar_multiplier"]
        sample_count = old["sample_count"]

        per_hour_entry = {
            "hour": h,
            "weekday": weekday,
            "predicted_consumption_kw": p["predicted_consumption_kw"],
            "actual_consumption_kw": a["actual_consumption_kw"],
            "predicted_solar_kwh": p["predicted_solar_kwh"],
            "actual_solar_kwh": a["actual_solar_kwh"],
            "predicted_net_load_kw": p["predicted_net_load_kw"],
            "actual_net_load_kw": a["actual_net_load_kw"],
            "consumption_ratio": None,
            "solar_ratio": None,
        }

        # Consumption update
        pc = p["predicted_consumption_kw"]
        ac = a["actual_consumption_kw"]
        if pc is not None and ac is not None and pc >= min_kw and ac >= 0:
            ratio = ac / pc if pc != 0 else 1.0
            ratio = max(min_ratio, min(max_ratio, ratio))
            per_hour_entry["consumption_ratio"] = round(ratio, 4)
            cons_ratios.append(ratio)
            cons_abs_pct_errs.append(abs(ac - pc) / pc * 100.0)
            new_cons_mult = round(alpha * ratio + (1.0 - alpha) * new_cons_mult, 4)

        # Solar update — only if the prediction wasn't trivially zero (night).
        ps = p["predicted_solar_kwh"]
        asv = a["actual_solar_kwh"]
        if (
            ps is not None
            and asv is not None
            and ps >= min_kwh_solar
            and asv >= 0
        ):
            sratio = asv / ps if ps != 0 else 1.0
            sratio = max(min_ratio, min(max_ratio, sratio))
            per_hour_entry["solar_ratio"] = round(sratio, 4)
            sol_ratios.append(sratio)
            sol_abs_pct_errs.append(abs(asv - ps) / ps * 100.0)
            new_sol_mult = round(alpha * sratio + (1.0 - alpha) * new_sol_mult, 4)

        per_hour.append(per_hour_entry)

        # Even if neither signal moved the multipliers (insufficient data),
        # touching the row marks it as "seen at least once" so subsequent
        # reads find a stable identity multiplier rather than missing keys.
        if per_hour_entry["consumption_ratio"] is not None or per_hour_entry["solar_ratio"] is not None:
            sample_count += 1
            history_db.upsert_adjustment(
                weekday=weekday,
                hour=h,
                consumption_multiplier=new_cons_mult,
                solar_multiplier=new_sol_mult,
                sample_count=sample_count,
                db_path=db_path,
            )
            updated_buckets.append((weekday, h))

    cons_mape = round(statistics.mean(cons_abs_pct_errs), 2) if cons_abs_pct_errs else None
    sol_mape = round(statistics.mean(sol_abs_pct_errs), 2) if sol_abs_pct_errs else None
    cons_bias = (
        round(statistics.mean(cons_ratios) - 1.0, 4) if cons_ratios else None
    )
    sol_bias = (
        round(statistics.mean(sol_ratios) - 1.0, 4) if sol_ratios else None
    )

    notes = (
        f"updated {len(updated_buckets)} buckets across {len(overlap_hours)} hours"
        f" (alpha={alpha})"
    )
    history_db.log_reconciliation(
        target_date=target_date,
        hours_reconciled=len(overlap_hours),
        consumption_mape=cons_mape,
        solar_mape=sol_mape,
        consumption_bias=cons_bias,
        solar_bias=sol_bias,
        notes=notes,
        db_path=db_path,
    )

    return ReconcileResult(
        target_date=target_date,
        hours_reconciled=len(overlap_hours),
        consumption_mape=cons_mape,
        solar_mape=sol_mape,
        consumption_bias=cons_bias,
        solar_bias=sol_bias,
        per_hour=per_hour,
        updated_buckets=updated_buckets,
        notes=notes,
    )


# ── Pulling reality from Home Assistant ──────────────────────────────────


def pull_actuals_from_ha(
    base_url: str,
    token: str,
    target_date: date,
    db_path=None,
    fetcher: Optional[Callable[[str, str, date], dict]] = None,
) -> int:
    """Fetch yesterday's (or any past day's) hourly actuals from HA and store.

    ``fetcher`` is injectable so tests can supply synthetic data without an
    HA endpoint.  Defaults to :func:`api.fetch_actuals_for_date`.
    """
    if fetcher is None:
        from api import fetch_actuals_for_date as fetcher  # type: ignore

    payload = fetcher(base_url, token, target_date)
    consumption = payload.get("consumption_kw") or [None] * 24
    solar = payload.get("solar_kwh") or [None] * 24
    net = payload.get("net_load_kw") or [None] * 24

    weekday = target_date.weekday()
    rows = []
    for h in range(24):
        if consumption[h] is None and solar[h] is None and net[h] is None:
            continue
        rows.append(
            {
                "target_date": target_date.isoformat(),
                "hour": h,
                "weekday": weekday,
                "actual_consumption_kw": consumption[h],
                "actual_solar_kwh": solar[h],
                "actual_net_load_kw": net[h] if net[h] is not None else consumption[h],
            }
        )
    if not rows:
        return 0
    return history_db.upsert_actuals(rows, db_path=db_path)


def auto_reconcile_recent(
    base_url: str,
    token: str,
    days_back: int = 1,
    today: Optional[date] = None,
    db_path=None,
    fetcher: Optional[Callable[[str, str, date], dict]] = None,
) -> list[ReconcileResult]:
    """Background-job entrypoint: pull actuals for the last ``days_back``
    completed days and reconcile each against stored predictions.

    Yesterday's run is the typical case; passing ``days_back=7`` is useful
    after the service has been offline for a week.
    """
    today = today or datetime.now().date()
    results: list[ReconcileResult] = []
    for offset in range(1, days_back + 1):
        d = today - timedelta(days=offset)
        try:
            pull_actuals_from_ha(
                base_url, token, d, db_path=db_path, fetcher=fetcher,
            )
        except Exception as e:  # noqa: BLE001
            history_db.log_reconciliation(
                d.isoformat(), 0, None, None, None, None,
                notes=f"actual fetch failed: {e}", db_path=db_path,
            )
            continue
        results.append(reconcile_date(d.isoformat(), db_path=db_path))
    return results


# ── Report generation (prediction vs reality) ────────────────────────────


def build_report(
    target_date: str, db_path=None
) -> dict:
    """Side-by-side prediction-vs-reality view for one date.

    Returns a dict suitable for JSON serialisation containing per-hour rows
    and headline accuracy metrics.  Designed for the new ``GET /report``
    endpoint and for ad-hoc inspection from the CLI.
    """
    preds = history_db.latest_predictions_for_date(target_date, db_path=db_path)
    actuals = history_db.actuals_for_date(target_date, db_path=db_path)

    pred_by_hour = {p["hour"]: p for p in preds}
    act_by_hour = {a["hour"]: a for a in actuals}
    hours = sorted(set(pred_by_hour) | set(act_by_hour))

    rows = []
    cons_abs_pct_errs: list[float] = []
    sol_abs_pct_errs: list[float] = []
    net_abs_pct_errs: list[float] = []
    cost_pred = 0.0
    cost_actual = 0.0

    for h in hours:
        p = pred_by_hour.get(h, {})
        a = act_by_hour.get(h, {})
        pc = p.get("predicted_consumption_kw")
        ac = a.get("actual_consumption_kw")
        ps = p.get("predicted_solar_kwh")
        asv = a.get("actual_solar_kwh")
        pn = p.get("predicted_net_load_kw")
        an = a.get("actual_net_load_kw")
        price = p.get("price_plkwh")

        cons_err_pct = _pct_err(pc, ac)
        sol_err_pct = _pct_err(ps, asv)
        net_err_pct = _pct_err(pn, an)
        if cons_err_pct is not None:
            cons_abs_pct_errs.append(abs(cons_err_pct))
        if sol_err_pct is not None:
            sol_abs_pct_errs.append(abs(sol_err_pct))
        if net_err_pct is not None:
            net_abs_pct_errs.append(abs(net_err_pct))

        if price is not None and pn is not None:
            cost_pred += price * pn
        if price is not None and an is not None:
            cost_actual += price * an

        rows.append(
            {
                "hour": h,
                "weekday": p.get("weekday", a.get("weekday")),
                "price_plkwh": price,
                "predicted_consumption_kw": pc,
                "actual_consumption_kw": ac,
                "consumption_err_pct": cons_err_pct,
                "predicted_solar_kwh": ps,
                "actual_solar_kwh": asv,
                "solar_err_pct": sol_err_pct,
                "predicted_net_load_kw": pn,
                "actual_net_load_kw": an,
                "net_err_pct": net_err_pct,
            }
        )

    summary = {
        "consumption_mape": (
            round(statistics.mean(cons_abs_pct_errs), 2) if cons_abs_pct_errs else None
        ),
        "solar_mape": (
            round(statistics.mean(sol_abs_pct_errs), 2) if sol_abs_pct_errs else None
        ),
        "net_load_mape": (
            round(statistics.mean(net_abs_pct_errs), 2) if net_abs_pct_errs else None
        ),
        "predicted_net_cost_pln": round(cost_pred, 4),
        "actual_net_cost_pln": round(cost_actual, 4),
        "cost_delta_pln": round(cost_actual - cost_pred, 4),
        "hours_with_prediction": len(pred_by_hour),
        "hours_with_actual": len(act_by_hour),
        "hours_compared": len(set(pred_by_hour) & set(act_by_hour)),
    }

    return {
        "target_date": target_date,
        "summary": summary,
        "rows": rows,
    }


def _pct_err(predicted, actual) -> Optional[float]:
    if predicted is None or actual is None:
        return None
    if predicted == 0:
        return None if actual == 0 else 100.0
    return round((actual - predicted) / predicted * 100.0, 2)
