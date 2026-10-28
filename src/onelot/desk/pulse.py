"""Market pulse: a 0..1 sentiment score the desk can compute from data it already has.

Components (each 0 = bearish, 0.5 = neutral, 1 = bullish; missing ones are skipped):
    breadth   share of watchlist stocks up on the day and above session VWAP
    trend     share of tracked indices with EMA20 above EMA50 on their signal bars
    vix       India VIX versus its one-year range (low VIX scores bullish)
    pcr       NIFTY put/call open-interest ratio from the Dhan option chain
              (contrarian: high PCR = bullish). Only when the Data API is active;
              one request per 5 minutes, far inside the 1-per-3-seconds limit.

The composite is recorded into `sentiment_scores` (scope MARKET, source "pulse")
every 15 minutes while the market is open, so backtests can gate on it and the
operator's own scorer can be compared against it. This is a transparent, rule-based
baseline, not the custom scorer the operator is building.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from ..clock import now_ist
from ..data.schema import sentiment_scores
from ..data.store import load_bars

log = logging.getLogger(__name__)

PCR_CACHE_SEC = 300
RECORD_EVERY_MIN = 15


class MarketPulse:
    def __init__(self, engine: Engine, desk, adapter_getter, data_api_active) -> None:
        self.engine = engine
        self.desk = desk
        self._adapter_getter = adapter_getter
        self._data_api_active = data_api_active
        self._pcr_cache: dict = {"value": None, "at": None, "error": None, "expiry": None}
        self._vix_hist: pd.Series | None = None
        self._vix_hist_at: dt.datetime | None = None
        self._last_record: dt.datetime | None = None
        self._lock = threading.Lock()
        self.last: dict = {"score": None, "label": "no data", "components": {}, "at": None}

    # ── components ────────────────────────────────────────────────────────
    def _breadth(self) -> dict | None:
        snap = self.desk.snapshot() if self.desk else {}
        stocks = snap.get("stocks") or []
        rows = [s for s in stocks if (s.get("last") or {}).get("change_pct") is not None]
        if not rows:
            return None
        up = sum(1 for s in rows if s["last"]["change_pct"] > 0)
        above = [s for s in rows if (s.get("regime") or {}).get("vwap") and s["last"]["price"]]
        above_vwap = sum(1 for s in above if s["last"]["price"] > s["regime"]["vwap"])
        score = 0.5 * (up / len(rows)) + 0.5 * (above_vwap / len(above) if above else up / len(rows))
        return {"score": round(score, 3), "up": up, "n": len(rows), "above_vwap": above_vwap,
                "note": f"{up}/{len(rows)} up on the day, {above_vwap}/{len(above) or len(rows)} above VWAP"}

    def _trend(self) -> dict | None:
        snap = self.desk.snapshot() if self.desk else {}
        idx = [s for s in (snap.get("indices") or []) if (s.get("regime") or {}).get("trend")]
        if not idx:
            return None
        up = sum(1 for s in idx if s["regime"]["trend"] == "up")
        dn = sum(1 for s in idx if s["regime"]["trend"] == "down")
        score = 0.5 + 0.5 * (up - dn) / len(idx)
        return {"score": round(score, 3), "up": up, "down": dn, "n": len(idx),
                "note": f"{up} of {len(idx)} indices trending up (EMA20 > EMA50), {dn} down"}

    def _vix(self, now: dt.datetime) -> dict | None:
        last = self.desk._context_last("INDIAVIX") if self.desk else None
        if last is None:
            return None
        if self._vix_hist is None or self._vix_hist_at is None or (now - self._vix_hist_at) > dt.timedelta(hours=6):
            h = load_bars(self.engine, "INDIAVIX", "1d")
            self._vix_hist = h["close"].tail(250) if not h.empty else pd.Series(dtype=float)
            self._vix_hist_at = now
        hist = self._vix_hist
        if hist is None or hist.empty:
            pct = None
            score = float(np.clip(1.0 - (last - 10.0) / 20.0, 0.0, 1.0))
        else:
            pct = float((hist < last).mean() * 100)
            score = float(np.clip(1.0 - pct / 100.0, 0.0, 1.0))
        return {"score": round(score, 3), "vix": round(float(last), 2), "percentile_1y": None if pct is None else round(pct, 1),
                "note": (f"VIX {last:.2f} at the {pct:.0f}th percentile of the last year" if pct is not None
                         else f"VIX {last:.2f} (no history for a percentile)")}

    def _pcr(self, now: dt.datetime) -> dict | None:
        adapter = self._adapter_getter()
        if adapter is None or not self._data_api_active():
            return {"score": None, "value": None, "note": "needs the Dhan Data API (option chain)"}
        at = self._pcr_cache.get("at")
        if at is None or (now - at).total_seconds() > PCR_CACHE_SEC:
            try:
                exp = (adapter.expiry_list_raw(13, "IDX_I") or [None])[0]
                snap = adapter.option_chain("NIFTY", exp, 13, "IDX_I") if exp else None
                put_oi = sum((r.oi or 0) for r in (snap.rows if snap else []) if r.opt_type == "PE")
                call_oi = sum((r.oi or 0) for r in (snap.rows if snap else []) if r.opt_type == "CE")
                val = (put_oi / call_oi) if call_oi else None
                self._pcr_cache = {"value": val, "at": now, "error": None, "expiry": exp}
            except Exception as exc:  # noqa: BLE001
                self._pcr_cache = {"value": self._pcr_cache.get("value"), "at": now, "error": f"{type(exc).__name__}: {str(exc)[:100]}",
                             "expiry": self._pcr_cache.get("expiry")}
        val = self._pcr_cache.get("value")
        if val is None:
            return {"score": None, "value": None, "note": self._pcr_cache.get("error") or "no chain yet"}
        score = float(np.clip((val - 0.7) / 0.8, 0.0, 1.0))      # 0.7 -> bearish extreme, 1.5 -> bullish
        return {"score": round(score, 3), "value": round(val, 3), "expiry": self._pcr_cache.get("expiry"),
                "note": f"NIFTY {self._pcr_cache.get('expiry')} PCR {val:.2f} (contrarian: high = bullish)"}

    # ── composite ─────────────────────────────────────────────────────────
    def compute(self, now: dt.datetime | None = None) -> dict:
        now = now or now_ist()
        with self._lock:
            comps = {"breadth": self._breadth(), "trend": self._trend(), "vix": self._vix(now), "pcr": self._pcr(now)}
            weights = {"breadth": 0.35, "trend": 0.3, "vix": 0.15, "pcr": 0.2}
            num = den = 0.0
            for k, c in comps.items():
                if c and c.get("score") is not None:
                    num += weights[k] * c["score"]
                    den += weights[k]
            score = round(num / den, 3) if den else None
            label = ("no data" if score is None else "bullish" if score >= 0.6 else "bearish" if score <= 0.4 else "chop")
            self.last = {"score": score, "label": label, "components": comps, "at": now.isoformat(),
                         "scale": "0 = bearish, 0.5 = chop, 1 = bullish",
                         "signed": None if score is None else round((score - 0.5) * 2, 3)}
            return self.last

    def maybe_record(self, now: dt.datetime, market_open: bool) -> None:
        if not market_open:
            return
        if self._last_record and (now - self._last_record) < dt.timedelta(minutes=RECORD_EVERY_MIN):
            return
        res = self.compute(now)
        if res["score"] is None:
            return
        self._last_record = now
        try:
            with self.engine.begin() as conn:
                conn.execute(sentiment_scores.insert().values(
                    ts=now, scope="MARKET", symbol=None, score=res["signed"], confidence=0.5, horizon="intraday",
                    source="pulse", note=f"{res['label']}; " + "; ".join(
                        f"{k}={c['score']}" for k, c in res["components"].items() if c and c.get("score") is not None),
                    created_at=now))
        except Exception:  # noqa: BLE001
            log.exception("pulse record failed")
