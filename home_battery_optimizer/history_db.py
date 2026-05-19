"""SQLite storage for prediction/reality tracking and learned multipliers.

Three tables drive the feedback loop:

* ``predictions``       — one row per (target_date, hour, recorded_at): what
                          the optimizer expected for that hour at the moment
                          it was asked.  The latest row per (date, hour)
                          wins during reconciliation.
* ``actuals``           — one row per (target_date, hour): what HA history
                          tells us actually happened.
* ``adjustments``       — one row per (weekday, hour): the rolling
                          consumption/solar multipliers learned by comparing
                          predictions against actuals.  These get folded
                          into the optimizer's net-load on every run unless
                          ``use_adjustments=false`` is supplied.

A small ``reconciliation_log`` records each pass so the dashboard can show
"last reconciled at X, MAPE=Y" without re-deriving it from scratch.

The DB file lives at ``$BATTERY_HISTORY_DB`` (default:
``home_battery_optimizer/data/history.db``).  The schema is created on first
open so deployment is "just point at a directory".
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional


# ── Connection management ──────────────────────────────────────────────────


_DEFAULT_DB_PATH = Path(__file__).parent / "data" / "history.db"
_LOCK = threading.Lock()


def default_db_path() -> Path:
    raw = os.environ.get("BATTERY_HISTORY_DB")
    return Path(raw) if raw else _DEFAULT_DB_PATH


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at              TEXT    NOT NULL,
    target_date              TEXT    NOT NULL,
    hour                     INTEGER NOT NULL,
    weekday                  INTEGER NOT NULL,
    predicted_consumption_kw REAL,
    predicted_solar_kwh      REAL,
    predicted_net_load_kw    REAL,
    price_plkwh              REAL,
    adjustments_applied      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_predictions_target
    ON predictions (target_date, hour);

CREATE INDEX IF NOT EXISTS idx_predictions_recorded
    ON predictions (recorded_at);

CREATE TABLE IF NOT EXISTS actuals (
    target_date            TEXT    NOT NULL,
    hour                   INTEGER NOT NULL,
    weekday                INTEGER NOT NULL,
    actual_consumption_kw  REAL,
    actual_solar_kwh       REAL,
    actual_net_load_kw     REAL,
    fetched_at             TEXT    NOT NULL,
    PRIMARY KEY (target_date, hour)
);

CREATE TABLE IF NOT EXISTS adjustments (
    weekday                INTEGER NOT NULL,
    hour                   INTEGER NOT NULL,
    consumption_multiplier REAL    NOT NULL DEFAULT 1.0,
    solar_multiplier       REAL    NOT NULL DEFAULT 1.0,
    sample_count           INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT    NOT NULL,
    PRIMARY KEY (weekday, hour)
);

CREATE TABLE IF NOT EXISTS reconciliation_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date         TEXT    NOT NULL,
    reconciled_at       TEXT    NOT NULL,
    hours_reconciled    INTEGER NOT NULL,
    consumption_mape    REAL,
    solar_mape          REAL,
    consumption_bias    REAL,
    solar_bias          REAL,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_recon_log_date
    ON reconciliation_log (target_date);
"""


@contextmanager
def open_db(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open the history DB, creating the schema on first use."""
    db_path = Path(path) if path else default_db_path()
    _ensure_parent(db_path)
    with _LOCK:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Predictions ────────────────────────────────────────────────────────────


def record_predictions(
    rows: Iterable[dict],
    db_path: Optional[Path] = None,
    recorded_at: Optional[str] = None,
) -> int:
    """Append per-hour predictions for a single optimizer run.

    Each row must contain:
        target_date (str, YYYY-MM-DD), hour (0..23), weekday (0..6),
        predicted_consumption_kw, predicted_solar_kwh,
        predicted_net_load_kw, price_plkwh, adjustments_applied (bool).

    Returns the number of rows inserted.
    """
    rows = list(rows)
    if not rows:
        return 0
    ts = recorded_at or _now_iso()
    with open_db(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO predictions
                (recorded_at, target_date, hour, weekday,
                 predicted_consumption_kw, predicted_solar_kwh,
                 predicted_net_load_kw, price_plkwh, adjustments_applied)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    ts,
                    r["target_date"],
                    int(r["hour"]),
                    int(r["weekday"]),
                    _opt_float(r.get("predicted_consumption_kw")),
                    _opt_float(r.get("predicted_solar_kwh")),
                    _opt_float(r.get("predicted_net_load_kw")),
                    _opt_float(r.get("price_plkwh")),
                    1 if r.get("adjustments_applied") else 0,
                )
                for r in rows
            ],
        )
    return len(rows)


def latest_predictions_for_date(
    target_date: str, db_path: Optional[Path] = None
) -> list[dict]:
    """Return the latest (one row per hour) prediction for ``target_date``.

    "Latest" means the row with the highest ``recorded_at`` for each hour,
    matching the optimizer's "what we were planning when we last looked".
    """
    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            SELECT p.*
              FROM predictions p
              JOIN (
                  SELECT hour, MAX(recorded_at) AS latest
                    FROM predictions
                   WHERE target_date = ?
                   GROUP BY hour
              ) m
                ON p.hour = m.hour
               AND p.recorded_at = m.latest
             WHERE p.target_date = ?
             ORDER BY p.hour ASC
            """,
            (target_date, target_date),
        )
        return [dict(row) for row in cur.fetchall()]


# ── Actuals ────────────────────────────────────────────────────────────────


def upsert_actuals(
    rows: Iterable[dict],
    db_path: Optional[Path] = None,
    fetched_at: Optional[str] = None,
) -> int:
    """Write the actual-consumption/solar values for a date.

    Each row: target_date, hour, weekday, actual_consumption_kw,
    actual_solar_kwh, actual_net_load_kw.
    """
    rows = list(rows)
    if not rows:
        return 0
    ts = fetched_at or _now_iso()
    with open_db(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO actuals
                (target_date, hour, weekday,
                 actual_consumption_kw, actual_solar_kwh, actual_net_load_kw,
                 fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_date, hour) DO UPDATE SET
                weekday               = excluded.weekday,
                actual_consumption_kw = excluded.actual_consumption_kw,
                actual_solar_kwh      = excluded.actual_solar_kwh,
                actual_net_load_kw    = excluded.actual_net_load_kw,
                fetched_at            = excluded.fetched_at
            """,
            [
                (
                    r["target_date"],
                    int(r["hour"]),
                    int(r["weekday"]),
                    _opt_float(r.get("actual_consumption_kw")),
                    _opt_float(r.get("actual_solar_kwh")),
                    _opt_float(r.get("actual_net_load_kw")),
                    ts,
                )
                for r in rows
            ],
        )
    return len(rows)


def actuals_for_date(
    target_date: str, db_path: Optional[Path] = None
) -> list[dict]:
    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM actuals WHERE target_date = ? ORDER BY hour ASC",
            (target_date,),
        )
        return [dict(row) for row in cur.fetchall()]


# ── Adjustments ────────────────────────────────────────────────────────────


def get_adjustments(
    db_path: Optional[Path] = None,
) -> dict[tuple[int, int], dict]:
    """Return ``{(weekday, hour): {consumption_multiplier, solar_multiplier, ...}}``.

    Missing buckets are simply absent; callers should default to 1.0.
    """
    out: dict[tuple[int, int], dict] = {}
    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT weekday, hour, consumption_multiplier, solar_multiplier, "
            "       sample_count, updated_at "
            "  FROM adjustments"
        )
        for row in cur.fetchall():
            out[(row["weekday"], row["hour"])] = {
                "consumption_multiplier": row["consumption_multiplier"],
                "solar_multiplier": row["solar_multiplier"],
                "sample_count": row["sample_count"],
                "updated_at": row["updated_at"],
            }
    return out


def upsert_adjustment(
    weekday: int,
    hour: int,
    consumption_multiplier: float,
    solar_multiplier: float,
    sample_count: int,
    db_path: Optional[Path] = None,
) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO adjustments
                (weekday, hour, consumption_multiplier, solar_multiplier,
                 sample_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(weekday, hour) DO UPDATE SET
                consumption_multiplier = excluded.consumption_multiplier,
                solar_multiplier       = excluded.solar_multiplier,
                sample_count           = excluded.sample_count,
                updated_at             = excluded.updated_at
            """,
            (
                int(weekday),
                int(hour),
                float(consumption_multiplier),
                float(solar_multiplier),
                int(sample_count),
                _now_iso(),
            ),
        )


def reset_adjustments(db_path: Optional[Path] = None) -> int:
    """Erase all learned multipliers (handy for tests / "start over" buttons)."""
    with open_db(db_path) as conn:
        cur = conn.execute("DELETE FROM adjustments")
        return cur.rowcount


# ── Reconciliation log ─────────────────────────────────────────────────────


def log_reconciliation(
    target_date: str,
    hours_reconciled: int,
    consumption_mape: Optional[float],
    solar_mape: Optional[float],
    consumption_bias: Optional[float],
    solar_bias: Optional[float],
    notes: str = "",
    db_path: Optional[Path] = None,
) -> int:
    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO reconciliation_log
                (target_date, reconciled_at, hours_reconciled,
                 consumption_mape, solar_mape,
                 consumption_bias, solar_bias, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_date,
                _now_iso(),
                int(hours_reconciled),
                _opt_float(consumption_mape),
                _opt_float(solar_mape),
                _opt_float(consumption_bias),
                _opt_float(solar_bias),
                notes,
            ),
        )
        return cur.lastrowid or 0


def recent_reconciliations(
    limit: int = 20, db_path: Optional[Path] = None
) -> list[dict]:
    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM reconciliation_log "
            " ORDER BY reconciled_at DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(row) for row in cur.fetchall()]


def reconciled_dates(db_path: Optional[Path] = None) -> list[str]:
    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT DISTINCT target_date FROM actuals "
            " ORDER BY target_date DESC"
        )
        return [row["target_date"] for row in cur.fetchall()]


# ── Misc helpers ───────────────────────────────────────────────────────────


def _opt_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
