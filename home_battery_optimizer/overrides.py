"""User-supplied overrides for the optimizer pipeline.

This module mirrors the most useful GBB Optimizer (https://gbboptimizer.gbbsoft.pl/Manual)
configuration knobs that make sense for a non-prosument LiFePo4 + Pstryk
installation. The exposed knobs are:

* ``consumption_scale``     — multiplier on the learned weekday×hour profile
                              ("we're going to be away" / "guests this week").
* ``consumption_profile``   — full {weekday(0=Mon)…6: [24 kW]} replacement,
                              equivalent to GBB's "Profile of Loads" manual entry.
* ``load_override_kw``      — per-horizon-hour float list that REPLACES the
                              net load entirely (after solar netting and any
                              other overrides).  Useful for one-off "force my
                              numbers" runs.
* ``extra_loads``           — list of planned one-shot loads (hour, kw,
                              duration_h).  Added on top of the learned/
                              overridden profile.  Mirrors GBB Extra Loads.
* ``solar_scale``           — correction factor applied to the PV forecast
                              (GBB's "correction factor" knob — drift between
                              forecast.solar / solcast and reality).
* ``solar_override_kwh``    — per-horizon-hour replacement for the PV
                              forecast (length must match prices).
* ``full_charge_days``      — set of day-of-month integers (1..31) where
                              ``max_soc`` is forced to 1.0 — GBB's "list of
                              days of month when 'Max battery SOC %' will be
                              100%" feature.  Used for periodic balance cycles.
* ``max_soc_pct``           — default MaxSOC in percent (GBB recommends 90%
                              as a buffer against forecast error; we keep 95%
                              as default to match prior behaviour).
* ``min_soc_pct``           — default MinSOC in percent (DoD floor).
* ``per_date_target_soc``   — ``{"YYYY-MM-DD": pct}`` — pin a specific day's
                              end-of-day target SOC.  Overrides the global
                              target_soc for that date.
* ``dry_run``               — flag the response as advisory; the caller is
                              expected NOT to send the decisions to the
                              inverter.  Mirrors GBB Test mode.

Parsing helpers accept simple text formats so the same overrides can come
from CLI flags, REST query parameters, or JSON bodies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class OptimizerOverrides:
    """Bundle of user-supplied tweaks that shape the optimizer's inputs."""

    consumption_scale: float = 1.0
    consumption_profile: Optional[dict[int, list[float]]] = None
    load_override_kw: Optional[list[float]] = None
    extra_loads: list[dict] = field(default_factory=list)
    solar_scale: float = 1.0
    solar_override_kwh: Optional[list[float]] = None
    full_charge_days: set[int] = field(default_factory=set)
    max_soc_pct: float = 95.0
    min_soc_pct: float = 10.0
    per_date_target_soc: dict[str, float] = field(default_factory=dict)
    dry_run: bool = False

    # Adjustments learned from past prediction-vs-reality reconciliation.
    # Keys are (weekday, hour); values are multipliers ≥ 0.  Default 1.0
    # when a bucket is absent.  These are NOT user-supplied — the REST
    # layer populates them from history_db when ``use_adjustments=true``.
    consumption_adjustment: Optional[dict[tuple[int, int], float]] = None
    solar_adjustment: Optional[dict[tuple[int, int], float]] = None

    # ── Resolution helpers ─────────────────────────────────────────────────

    def max_soc_for_date(self, day: date) -> float:
        """Resolve MaxSOC for a calendar date (1.0 on scheduled balance days)."""
        if day.day in self.full_charge_days:
            return 1.0
        return self.max_soc_pct / 100.0

    def min_soc(self) -> float:
        return self.min_soc_pct / 100.0

    def target_soc_for_date(
        self, day: Optional[date], fallback_pct: Optional[float]
    ) -> Optional[float]:
        """Resolve EOD target SOC for a given calendar date.

        Per-date overrides win over the caller-supplied fallback (which itself
        may be None when the optimizer should pick freely).
        """
        if day is not None:
            iso = day.isoformat()
            if iso in self.per_date_target_soc:
                return self.per_date_target_soc[iso] / 100.0
        return fallback_pct / 100.0 if fallback_pct is not None else None

    def summary(self) -> dict:
        """Return a JSON-friendly summary of which overrides are active."""
        return {
            "consumption_scale": self.consumption_scale,
            "consumption_profile_overridden": self.consumption_profile is not None,
            "load_override_active": self.load_override_kw is not None,
            "extra_loads": self.extra_loads,
            "solar_scale": self.solar_scale,
            "solar_override_active": self.solar_override_kwh is not None,
            "full_charge_days": sorted(self.full_charge_days),
            "max_soc_pct": self.max_soc_pct,
            "min_soc_pct": self.min_soc_pct,
            "per_date_target_soc": self.per_date_target_soc,
            "dry_run": self.dry_run,
            "consumption_adjustment_buckets": (
                len(self.consumption_adjustment)
                if self.consumption_adjustment is not None
                else 0
            ),
            "solar_adjustment_buckets": (
                len(self.solar_adjustment)
                if self.solar_adjustment is not None
                else 0
            ),
        }

    def consumption_adjustment_for(self, weekday: int, hour: int) -> float:
        if self.consumption_adjustment is None:
            return 1.0
        return self.consumption_adjustment.get((weekday, hour), 1.0)

    def solar_adjustment_for(self, weekday: int, hour: int) -> float:
        if self.solar_adjustment is None:
            return 1.0
        return self.solar_adjustment.get((weekday, hour), 1.0)


# ── Parsers (CLI / REST friendly) ──────────────────────────────────────────


def parse_csv_floats(raw: Optional[str]) -> Optional[list[float]]:
    """Parse "1.2,1.3,..." into a list of floats; ``None``/empty → ``None``."""
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    try:
        return [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"Bad float list '{raw}': {e}") from e


def parse_csv_ints(raw: Optional[str]) -> set[int]:
    """Parse "1,15,28" into ``{1,15,28}``; empty/None → empty set."""
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError as e:
            raise ValueError(f"Bad int '{part}' in '{raw}': {e}") from e
        if not (1 <= v <= 31):
            raise ValueError(f"Day-of-month must be 1..31 (got {v})")
        out.add(v)
    return out


def parse_extra_loads(raw: Optional[str]) -> list[dict]:
    """Parse extra loads.

    Two formats accepted:
        * JSON: ``[{"hour": 14, "kw": 2.0, "duration_h": 2}, ...]``
        * Compact: ``"14:2.0:2,18:1.5:1"`` (hour:kw[:duration_h], duration default 1)

    Returns a list of validated dicts.
    """
    if not raw:
        return []
    raw = raw.strip()
    loads: list[dict] = []

    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Bad JSON for extra_loads: {e}") from e
        if not isinstance(parsed, list):
            raise ValueError("extra_loads JSON must be a list")
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("Each extra_loads entry must be an object")
            loads.append(_validate_extra_load(item))
    else:
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(":")
            if len(parts) not in (2, 3):
                raise ValueError(
                    f"Bad extra_loads entry '{chunk}' — expected hour:kw[:duration_h]"
                )
            try:
                hour = int(parts[0])
                kw = float(parts[1])
                duration_h = int(parts[2]) if len(parts) == 3 else 1
            except ValueError as e:
                raise ValueError(f"Bad numbers in extra_loads entry '{chunk}': {e}") from e
            loads.append(_validate_extra_load({"hour": hour, "kw": kw, "duration_h": duration_h}))
    return loads


def _validate_extra_load(item: dict) -> dict:
    hour = item.get("hour")
    kw = item.get("kw")
    duration_h = item.get("duration_h", 1)
    if not isinstance(hour, int) or not (0 <= hour < 24):
        raise ValueError(f"extra_loads.hour must be int 0..23 (got {hour!r})")
    if not isinstance(kw, (int, float)) or kw < 0:
        raise ValueError(f"extra_loads.kw must be non-negative number (got {kw!r})")
    if not isinstance(duration_h, int) or duration_h < 1 or duration_h > 24:
        raise ValueError(
            f"extra_loads.duration_h must be int 1..24 (got {duration_h!r})"
        )
    # Optional day-of-horizon index (0 = first day, 1 = second day, …)
    day_idx = item.get("day", 0)
    if not isinstance(day_idx, int) or day_idx < 0:
        raise ValueError(f"extra_loads.day must be non-negative int (got {day_idx!r})")
    return {"hour": hour, "kw": float(kw), "duration_h": duration_h, "day": day_idx}


def parse_consumption_profile(raw: Optional[str]) -> Optional[dict[int, list[float]]]:
    """Parse a JSON ``{weekday: [24 floats]}`` map; ``None``/empty → ``None``.

    Weekday keys may be ints 0..6 (Mon..Sun) or strings ``"0"``..``"6"``.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Bad JSON for consumption_profile: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("consumption_profile must be a JSON object")

    out: dict[int, list[float]] = {}
    for k, v in parsed.items():
        try:
            wd = int(k)
        except (ValueError, TypeError) as e:
            raise ValueError(f"consumption_profile key '{k}' is not an int weekday") from e
        if not (0 <= wd <= 6):
            raise ValueError(f"consumption_profile weekday must be 0..6 (got {wd})")
        if not isinstance(v, list) or len(v) != 24:
            raise ValueError(
                f"consumption_profile[{wd}] must be a list of exactly 24 numbers"
            )
        try:
            row = [float(x) for x in v]
        except (ValueError, TypeError) as e:
            raise ValueError(f"consumption_profile[{wd}] has non-numeric entries") from e
        if any(x < 0 for x in row):
            raise ValueError(f"consumption_profile[{wd}] has negative values")
        out[wd] = row
    return out


def parse_per_date_target_soc(raw: Optional[str]) -> dict[str, float]:
    """Parse ``"2026-05-19:80,2026-05-20:30"`` or JSON ``{"YYYY-MM-DD": pct}``."""
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Bad JSON for per_date_target_soc: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError("per_date_target_soc JSON must be an object")
        return {str(k): float(v) for k, v in parsed.items()}

    out: dict[str, float] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"Bad per_date_target_soc entry '{chunk}'")
        d, pct = chunk.split(":", 1)
        try:
            val = float(pct)
        except ValueError as e:
            raise ValueError(f"Bad SOC pct in '{chunk}': {e}") from e
        if not (0 <= val <= 100):
            raise ValueError(f"per_date_target_soc value must be 0..100 (got {val})")
        out[d.strip()] = val
    return out


# ── Top-level builder ──────────────────────────────────────────────────────


def build_overrides_from_query(args: dict) -> OptimizerOverrides:
    """Translate a flat string-valued mapping (query params / CLI dict) into
    an :class:`OptimizerOverrides`.

    Unknown keys are ignored so the caller can pass the raw query dict.
    """

    def _f(name: str, default: float) -> float:
        v = args.get(name)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except ValueError as e:
            raise ValueError(f"{name}: expected number, got {v!r}") from e

    def _b(name: str, default: bool = False) -> bool:
        v = args.get(name)
        if v is None:
            return default
        return str(v).lower() in ("true", "1", "yes", "on")

    ov = OptimizerOverrides(
        consumption_scale=_f("consumption_scale", 1.0),
        consumption_profile=parse_consumption_profile(args.get("consumption_profile")),
        load_override_kw=parse_csv_floats(args.get("load_override_kw")),
        extra_loads=parse_extra_loads(args.get("extra_loads")),
        solar_scale=_f("solar_scale", 1.0),
        solar_override_kwh=parse_csv_floats(args.get("solar_override_kwh")),
        full_charge_days=parse_csv_ints(args.get("full_charge_days")),
        max_soc_pct=_f("max_soc_pct", 95.0),
        min_soc_pct=_f("min_soc_pct", 10.0),
        per_date_target_soc=parse_per_date_target_soc(args.get("per_date_target_soc")),
        dry_run=_b("dry_run", False),
    )

    if not (0.0 < ov.consumption_scale <= 10.0):
        raise ValueError("consumption_scale must be in (0, 10]")
    if not (0.0 < ov.solar_scale <= 10.0):
        raise ValueError("solar_scale must be in (0, 10]")
    if not (0.0 <= ov.min_soc_pct < ov.max_soc_pct <= 100.0):
        raise ValueError(
            f"SOC bounds invalid: min={ov.min_soc_pct}, max={ov.max_soc_pct}"
        )
    return ov
