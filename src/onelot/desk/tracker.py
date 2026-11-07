"""One tracked instrument: fine bars in, per-strategy verdicts out."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np
import pandas as pd

from ..clock import IST
from ..data.resample import minutes_of, resample_bars
from ..data.universe import InstrumentSpec, SeriesSpec
from ..strategies.base import Context
from ..strategies.registry import REGISTRY, get_strategy
from .config import DeskInstrumentCfg, StrategySlotCfg
from .rules import lab_entry_risk

log = logging.getLogger(__name__)

MIN_BARS = 60


@dataclass
class Verdict:
    direction: str = "none"  # long | short | none
    reason: str = ""
    bar_ts: dt.datetime | None = None
    stop: float | None = None
    close: float | None = None
    strength: float | None = None
    reasons: list[str] = field(default_factory=list)
    fresh: bool = False


@dataclass
class StrategySlot:
    cfg: StrategySlotCfg
    strategy: object
    label: str
    tf_min: int
    last_bar_ts: dt.datetime | None = None
    last_verdict: Verdict = field(default_factory=Verdict)
    last_eval_at: dt.datetime | None = None
    signals_today: int = 0
    # when this slot's last alert's paper trade closed: the Lab's cooldown counts from it
    last_exit_at: dt.datetime | None = None
    last_features: dict = field(default_factory=dict)
    last_flags: dict = field(
        default_factory=dict
    )  # long_entry/short_entry/long_exit/short_exit of the last bar
    error: str | None = None

    def exit_advice(self, side: str) -> str | None:
        """What the strategy says about an OPEN position on its latest closed bar."""
        f = self.last_flags
        if not f:
            return None
        if f.get(f"{side}_exit"):
            return "SIGNAL_EXIT"
        if f.get("short_entry" if side == "long" else "long_entry"):
            return "OPPOSITE_SIGNAL"
        return None

    def as_dict(self) -> dict:
        v = self.last_verdict
        return {
            "name": self.cfg.name,
            "label": self.label,
            "timeframe": self.cfg.timeframe,
            "params": dict(self.cfg.params),
            "last_bar_ts": self.last_bar_ts.isoformat() if self.last_bar_ts else None,
            "last_eval_at": self.last_eval_at.isoformat() if self.last_eval_at else None,
            "verdict": v.direction,
            "reason": v.reason,
            "fresh": v.fresh,
            "strength": v.strength,
            "signals_today": self.signals_today,
            "error": self.error,
            "features": self.last_features,
        }


class InstrumentTracker:
    def __init__(
        self,
        cfg: DeskInstrumentCfg,
        series: SeriesSpec,
        instrument: InstrumentSpec,
        fine_minutes: int,
    ) -> None:
        self.cfg = cfg
        self.series = series
        self.instrument = instrument
        self.fine_minutes = fine_minutes
        self.bars: pd.DataFrame = pd.DataFrame()
        self.slots: list[StrategySlot] = []
        for s in cfg.strategies:
            if s.name not in REGISTRY:
                log.warning("desk %s: unknown strategy %s skipped", cfg.key, s.name)
                continue
            params = {**s.params, "timeframe": s.timeframe}
            sess = instrument.session
            params.setdefault("entry_start", sess.get("first_entry", "09:30"))
            params.setdefault("entry_end", sess.get("last_entry", "14:45"))
            params.setdefault("exit_time", sess.get("square_off", "15:15"))
            try:
                strat = get_strategy(s.name, params)
            except Exception as exc:  # noqa: BLE001
                log.warning("desk %s: strategy %s rejected params: %s", cfg.key, s.name, exc)
                continue
            self.slots.append(
                StrategySlot(s, strat, REGISTRY[s.name].label, minutes_of(s.timeframe))
            )
        self.trades_today = 0
        self.consecutive_losses = 0
        self.pnl_today = 0.0
        self.last_error: str | None = None
        self.regime: dict = {}
        self.live_quote: dict | None = (
            None  # last WS tick (ltp, bid, ask, ts) when the Dhan socket is on
        )

    # ── bars ──────────────────────────────────────────────────────────────
    def update_bars(self, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        if self.bars.empty:
            self.bars = df
        else:
            merged = pd.concat([self.bars, df])
            self.bars = merged[~merged.index.duplicated(keep="last")].sort_index()
        cutoff = pd.Timestamp.now(tz=IST) - pd.Timedelta(days=70)
        self.bars = self.bars[self.bars.index >= cutoff]

    def last_price(self) -> float | None:
        q = self.live_quote
        if q and q.get("ltp"):
            return float(q["ltp"])
        return float(self.bars["close"].iloc[-1]) if not self.bars.empty else None

    def bars_for(self, timeframe: str, n: int = 400) -> pd.DataFrame:
        df = self.bars
        if df.empty:
            return df
        if timeframe and minutes_of(timeframe) != self.fine_minutes:
            df = resample_bars(df, timeframe)
        return df.tail(n)

    def last_bar_ts(self) -> dt.datetime | None:
        return self.bars.index[-1].to_pydatetime() if not self.bars.empty else None

    def session_change(self) -> tuple[float | None, float | None]:
        """(day change %, previous session close) from the fine bars."""
        if self.bars.empty:
            return None, None
        day = self.bars.index.normalize()
        closes = self.bars["close"].groupby(day).last()
        if len(closes) < 2:
            return None, None
        prev = float(closes.iloc[-2])
        return (float(closes.iloc[-1]) / prev - 1) * 100, prev

    def bar_age_sec(self, now: dt.datetime) -> float | None:
        ts = self.last_bar_ts()
        if ts is None:
            return None
        return (now - (ts + dt.timedelta(minutes=self.fine_minutes))).total_seconds()

    # ── evaluation ────────────────────────────────────────────────────────
    def evaluate(
        self, now: dt.datetime, market_bars: pd.DataFrame | None, entries_allowed: bool
    ) -> list[tuple[StrategySlot, Verdict]]:
        """Run every slot on its closed bars; return the slots whose LAST bar fired."""
        fired: list[tuple[StrategySlot, Verdict]] = []
        if self.bars.empty:
            return fired
        latest_close_at = self.bars.index[-1] + pd.Timedelta(minutes=self.fine_minutes)
        for slot in self.slots:
            try:
                v = self._evaluate_slot(slot, latest_close_at, market_bars, now)
            except Exception as exc:  # noqa: BLE001
                slot.error = f"{type(exc).__name__}: {str(exc)[:120]}"
                log.exception("desk %s %s failed", self.cfg.key, slot.cfg.name)
                continue
            slot.error = None
            if v.direction != "none" and v.fresh:
                held = _strategy_limit(slot, v)
                if held is not None:
                    v = Verdict("none", held, v.bar_ts, None, v.close, v.strength, [], v.fresh)
            slot.last_verdict = v
            slot.last_eval_at = now
            if v.direction != "none" and v.fresh and entries_allowed:
                fired.append((slot, v))
        return fired

    def _evaluate_slot(
        self,
        slot: StrategySlot,
        latest_close_at: pd.Timestamp,
        market_bars: pd.DataFrame | None,
        now: dt.datetime,
    ) -> Verdict:
        df = self.bars
        if slot.tf_min != self.fine_minutes:
            df = resample_bars(df, slot.cfg.timeframe)
            if not df.empty and df.index[-1] + pd.Timedelta(minutes=slot.tf_min) > latest_close_at:
                df = df.iloc[:-1]  # drop the still-forming bar
        if len(df) < MIN_BARS:
            return Verdict("none", f"warming up: {len(df)}/{MIN_BARS} {slot.cfg.timeframe} bars")
        bar_ts = df.index[-1].to_pydatetime()
        if slot.last_bar_ts is not None and bar_ts <= slot.last_bar_ts:
            v = slot.last_verdict
            return Verdict("none", "no new bar yet", bar_ts, None, v.close, v.strength, [], False)
        mkt = None
        if market_bars is not None and not market_bars.empty and self.series.key != "NIFTY":
            mkt = (
                market_bars
                if slot.tf_min == self.fine_minutes
                else resample_bars(market_bars, slot.cfg.timeframe)
            )
        ctx = Context(
            symbol=self.series.key,
            daily=None,
            market=mkt,
            extras={"has_volume": bool(self.series.has_volume)},
        )
        f = slot.strategy.generate(df, ctx)
        last = f.iloc[-1]
        slot.last_bar_ts = bar_ts
        # "Fresh" = this strategy bar completed within the last fine bar. With 2-minute
        # Yahoo bars a 5/15-minute bar never closes on an exact fine-bar boundary, so an
        # equality test (the Bank Nifty engine's rule on 1-minute data) would never fire.
        lag = latest_close_at - (df.index[-1] + pd.Timedelta(minutes=slot.tf_min))
        fresh = pd.Timedelta(0) <= lag <= pd.Timedelta(minutes=self.fine_minutes)
        self.regime = _regime_dict(last)
        slot.last_features = {
            k: _num(last.get(k))
            for k in (
                "adx",
                "rsi",
                "atr",
                "atr_trail",  # ATR(trail_atr_period): what the strategy trails with
                "atr_pct",
                "vwap",
                "ema20",
                "ema50",
                "rvol",
                "long_score",
                "short_score",
                "mkt_bias",
            )
        }
        slot.last_features["regime"] = str(last.get("regime", ""))
        slot.last_flags = {
            k: bool(last.get(k)) for k in ("long_entry", "short_entry", "long_exit", "short_exit")
        }
        long_e, short_e = bool(last.get("long_entry")), bool(last.get("short_entry"))
        close = float(last["close"])
        if long_e or short_e:
            side = "long" if long_e else "short"
            stop = _num(last.get(f"{side}_stop"))
            try:
                expl = slot.strategy.explain(f, len(f) - 1, side)
                reasons = list(expl.get("reasons") or [])
            except Exception:  # noqa: BLE001
                reasons = []
            if (
                stop is None
                or (side == "long" and stop >= close)
                or (side == "short" and stop <= close)
            ):
                return Verdict(
                    "none",
                    f"{side} setup without a usable stop",
                    bar_ts,
                    None,
                    close,
                    _num(last.get(f"{side}_score")),
                    reasons,
                    fresh,
                )
            cooling = _cooling_down(slot, df)
            if cooling is not None:
                return Verdict(
                    "none", cooling, bar_ts, None, close, _num(last.get(f"{side}_score")), [], fresh
                )
            # the Lab's entry risk rule: widen a too-tight stop, skip a too-wide one
            stop, skipped = lab_entry_risk(
                side, close, stop, _num(last.get("atr")), _num(last.get("d_atr")), _params(slot)
            )
            if stop is None:
                return Verdict(
                    "none",
                    f"{side} setup skipped by the strategy's risk rule: {skipped}",
                    bar_ts,
                    None,
                    close,
                    _num(last.get(f"{side}_score")),
                    reasons,
                    fresh,
                )
            return Verdict(
                side,
                "entry on the last closed bar"
                if fresh
                else "stale bar (not the one that just closed)",
                bar_ts,
                stop,
                close,
                _num(last.get(f"{side}_score")),
                reasons,
                fresh,
            )
        blockers = [
            c[4:]
            for c in ("blk_window", "blk_regime", "blk_mkt_long", "blk_mkt_short")
            if bool(last.get(c))
        ]
        why = "no setup on the last bar" + (
            f" (blocked: {', '.join(blockers)})" if blockers else ""
        )
        return Verdict("none", why, bar_ts, None, close, None, [], fresh)

    # ── reporting ─────────────────────────────────────────────────────────
    def as_dict(
        self, now: dt.datetime, source_name: str, delayed: bool, market_open: bool = True
    ) -> dict:
        chg, prev = self.session_change()
        age = self.bar_age_sec(now)
        expected = self.fine_minutes * 60 + (15 * 60 if delayed else 0) + 90
        if self.bars.empty:
            hstate, hnote = "NO DATA", "no bars received yet"
        elif not market_open:
            hstate, hnote = (
                "CLOSED",
                f"market closed; last bar {self.last_bar_ts().strftime('%d %b %H:%M')}",
            )
        elif age is not None and age > expected * 3:
            hstate, hnote = "STALE", f"last bar {age / 60:.0f} min old"
        elif age is not None and age > expected:
            hstate, hnote = "LAGGING", f"last bar {age / 60:.0f} min old"
        else:
            hstate, hnote = "OK", ("delayed feed (~15 min)" if delayed else "live")
        return {
            "key": self.cfg.key,
            "label": self.series.label,
            "kind": self.cfg.kind,
            "note": self.cfg.note,
            "tracked": self.instrument.key,
            "tracked_label": self.instrument.label,
            "unit": "1 share"
            if self.cfg.kind == "STOCK"
            else f"1 lot ({self.instrument.lot_size})",
            "last": {
                "price": self.last_price(),
                "change_pct": chg,
                "prev_close": prev,
                "bar_ts": self.last_bar_ts().isoformat() if self.last_bar_ts() else None,
                "age_sec": age,
                "live": self.live_quote,
            },
            "strategies": [s.as_dict() for s in self.slots],
            "regime": self.regime,
            "risk": {
                "trades_today": self.trades_today,
                "consecutive_losses": self.consecutive_losses,
                "pnl_today": self.pnl_today,
            },
            "health": {
                "state": hstate,
                "note": hnote,
                "bars": int(len(self.bars)),
                "source": source_name,
                "delayed": delayed,
                "resolution_min": self.fine_minutes,
                "last_error": self.last_error,
            },
        }


def _params(slot: StrategySlot) -> Any:
    """The slot strategy's parameters (StrategySlot types the strategy as `object`)."""
    return cast(Any, slot.strategy).p


def _strategy_limit(slot: StrategySlot, v: Verdict) -> str | None:
    """The strategy's OWN `max_trades_per_day`, as the Lab simulates it (not a risk
    governor): alerts per session. None when the entry may be alerted."""
    cap = int(getattr(_params(slot), "max_trades_per_day", 0) or 0)
    if cap and slot.signals_today >= cap:
        return f"the strategy's limit of {cap} trade(s) a day is reached"
    return None


def _cooling_down(slot: StrategySlot, bars: pd.DataFrame) -> str | None:
    """The Lab's `cooldown_bars`: a signal bar must come MORE than that many strategy bars
    after the bar its last trade exited in (counted across sessions, like the backtest)."""
    n = int(getattr(_params(slot), "cooldown_bars", 0) or 0)
    if not n or slot.last_exit_at is None or bars.empty:
        return None
    exit_i = int(bars.index.searchsorted(pd.Timestamp(slot.last_exit_at), side="right")) - 1
    if len(bars) - 1 - exit_i > n:
        return None
    return f"cooling down: {n} {slot.cfg.timeframe} bars after its last exit"


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else round(f, 4)


def _regime_dict(last: pd.Series) -> dict:
    adx, ema20, ema50 = _num(last.get("adx")), _num(last.get("ema20")), _num(last.get("ema50"))
    trend = (
        "up"
        if (ema20 is not None and ema50 is not None and ema20 > ema50)
        else ("down" if (ema20 is not None and ema50 is not None and ema20 < ema50) else "flat")
    )
    return {
        "regime": str(last.get("regime", "")),
        "adx": adx,
        "atr": _num(last.get("atr")),
        "atr_pct": _num(last.get("atr_pct")),
        "rsi": _num(last.get("rsi")),
        "vwap": _num(last.get("vwap")),
        "ema20": ema20,
        "ema50": ema50,
        "trend": trend,
        "chop": _num(last.get("chop")),
        "mkt_bias": _num(last.get("mkt_bias")),
    }
