"""Historical backfill: incremental, idempotent, resumable downloads into `bars`.

The corpus is keyed by SERIES (indices, stocks, spot FX). Yahoo is the keyless
default; Dhan takes over automatically once the Data API is active
(`data.provider: auto`). Each run only fetches what is missing, re-fetching
the last stored day in case it was partial.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import Engine

from ..clock import IST, parse_hhmm
from ..logging_setup import get_logger
from ..settings import current_settings
from .providers.yahoo import YahooProvider
from .store import available_resolutions, load_bars, save_bars
from .universe import SeriesSpec, load_specs

log = get_logger("data.backfill")

# What to pull from each provider. Dhan serves 1-minute bars for five years,
# so 1m + 1d is everything (coarser bars are derived). Yahoo caps intraday
# history per resolution, so each native resolution is pulled separately.
PLAN = {
    "dhan": ["1d", "1m"],
    "yahoo": ["1d", "60m", "15m", "5m", "2m"],
}


def choose_provider(preferred: str | None = None, adapter=None):
    """Dhan when asked for (or `auto`) and a Data-API-capable adapter is at hand;
    Yahoo otherwise."""
    mode = (preferred or current_settings().data.provider).lower()
    if mode in ("dhan", "auto") and adapter is not None:
        from .providers.dhan import DhanHistoryProvider

        return DhanHistoryProvider(adapter)
    if mode == "dhan":
        raise RuntimeError("Dhan provider selected but no authenticated Dhan adapter is available")
    return YahooProvider()


@dataclass
class BackfillResult:
    provider: str
    rows: int = 0
    items: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"provider": self.provider, "rows": self.rows, "items": self.items, "errors": self.errors}


def backfill(engine: Engine, provider=None, series_keys: list[str] | None = None,
             resolutions: list[str] | None = None, start: dt.date | None = None,
             end: dt.date | None = None, full: bool = False,
             progress: Callable[[float, str], None] | None = None,
             stop_flag: Callable[[], bool] | None = None) -> BackfillResult:
    provider = provider or choose_provider()
    series, _ = load_specs()
    specs: list[SeriesSpec] = list(series.values())
    if series_keys:
        wanted = {k.upper() for k in series_keys}
        specs = [s for s in specs if s.key in wanted]
    resolutions = resolutions or PLAN.get(provider.name, ["1d"])
    today = dt.datetime.now(IST).date()
    end = end or today
    hist_start = start or dt.date.fromisoformat(current_settings().data.history_start)
    result = BackfillResult(provider=provider.name)
    jobs = [(s, r) for s in specs for r in resolutions if r in provider.supported_resolutions()]
    for k, (spec, res) in enumerate(jobs, 1):
        if stop_flag and stop_flag():
            result.errors.append("stopped")
            break
        label = f"{spec.key} {res}"
        if progress:
            progress((k - 1) / max(1, len(jobs)), label)
        try:
            frm = hist_start
            earliest = provider.earliest_available(res, today)
            if earliest:
                frm = max(frm, earliest)
            if not full:
                have = available_resolutions(engine, spec.key).get(res)
                if have:
                    frm = max(frm, have[1].date())  # re-fetch the last stored day (may be partial)
            if frm > end:
                continue
            sess = spec.session_times
            session = (parse_hhmm(sess["open"]), parse_hhmm(sess["close"]))
            if provider.name == "dhan":
                df = provider.history(spec.key, spec.dhan_security_id, res, frm, end, session=session, spec=spec)
            else:
                if not spec.yahoo:
                    continue
                df = provider.history(spec.key, spec.yahoo, res, frm, end, session=session)
            n = save_bars(engine, spec.key, res, df, provider.name)
            result.rows += n
            result.items.append({"series": spec.key, "resolution": res, "from": str(frm),
                                 "to": str(end), "rows": n})
            log.info("backfill %-12s %-4s %s..%s rows=%d", spec.key, res, frm, end, n)
        except Exception as exc:  # noqa: BLE001
            msg = f"{label}: {exc}"
            result.errors.append(msg)
            log.warning("backfill failed %s", msg)
    if progress:
        progress(1.0, "done")
    return result


def quality_report(engine: Engine, resolution: str = "5m") -> pd.DataFrame:
    """Per-series completeness and anomaly flags for one resolution."""
    series, _ = load_specs()
    rows = []
    for spec in series.values():
        df = load_bars(engine, spec.key, resolution)
        if df.empty:
            rows.append({"series": spec.key, "days": 0})
            continue
        sess = spec.session_times
        o, c = parse_hhmm(sess["open"]), parse_hhmm(sess["close"])
        mins = (c.hour * 60 + c.minute) - (o.hour * 60 + o.minute)
        per_day_expected = mins // int(resolution[:-1]) if resolution.endswith("m") else 1
        by_day = df.groupby(df.index.date).size()
        short_days = int((by_day < per_day_expected * 0.9).sum())
        daily_close = df["close"].groupby(df.index.date).last()
        daily_open = df["open"].groupby(df.index.date).first()
        gaps = (daily_open / daily_close.shift(1) - 1).abs()
        rows.append({
            "series": spec.key, "first": str(df.index[0])[:16], "last": str(df.index[-1])[:16],
            "days": int(len(by_day)), "bars": int(len(df)), "short_days": short_days,
            "zero_volume_bars": int((df["volume"] <= 0).sum()) if spec.has_volume else None,
            "max_gap_pct": round(float(gaps.max() * 100), 2) if len(gaps) > 1 else 0.0,
            "suspect_corporate_action": bool((gaps > 0.18).any()) if spec.kind == "STOCK" else False,
        })
    return pd.DataFrame(rows)
