"""One tracked instrument: fine bars in, per-strategy verdicts out."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np
import pandas as pd

from ..clock import IST
from ..data.hygiene import clean_intraday, consistent_daily
from ..data.resample import minutes_of, resample_bars
from ..data.universe import InstrumentSpec, SeriesSpec
from ..indicators import mtf
from ..strategies import mtf_support as ms
from ..strategies.base import Context
from ..strategies.registry import REGISTRY, get_strategy
from .config import DeskInstrumentCfg, StrategySlotCfg
from .rules import lab_entry_risk

log = logging.getLogger(__name__)

MIN_BARS = 60
#: calendar days between the last stored daily bar and the first live one before the daily
#: trend is flagged (a long weekend with a holiday is five)
HISTORY_GAP_DAYS = 5
#: bars of a timeframe before its Supertrend has a reading
_WARM_BARS = mtf.WARMUP_FACTOR * mtf.ST_PERIOD


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
    #: the strategy trails the stop along its own line (it gives long_trail / short_trail)
    line_trail: bool = False
    #: side -> the level that line allows the stop to stand at after the last closed bar
    last_trail: dict = field(default_factory=dict)
    error: str | None = None

    def trail_level(self, side: str) -> float | None:
        """Where the strategy's own line lets the stop of an OPEN `side` position stand after
        its latest closed bar. None: no line trail, or the line is not on that side now (the
        stop then stays where it is). The level only ever tightens a stop: whoever follows it
        takes it when it is nearer the price than the stop in force, never otherwise."""
        if not self.line_trail:
            return None
        level = self.last_trail.get(side)
        return float(level) if level is not None else None

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

    def trail_view(self) -> dict | None:
        """The strategy's own trail as a card shows it: the timeframe of the line and where it
        lets a stop stand after the last closed bar, per side (None on a side the line is not
        on now). None for a slot that trails by ATR or not at all."""
        if not self.line_trail:
            return None
        tf = getattr(self.strategy, "trail_timeframe", None)
        return {
            "timeframe": str(tf) if tf else self.cfg.timeframe,
            "long": self.trail_level("long"),
            "short": self.trail_level("short"),
        }

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
            "trail": self.trail_view(),
            "enabled": self.cfg.enabled,
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
            if s.product != "intraday":
                continue  # a delivery slot (US-067) is the delivery desk's, not a strategy here
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
        # the card's regime comes from the single-timeframe slots when there is one: a
        # multi-timeframe slot looks every few minutes and would overwrite it with its own
        self._plain_slots = any(not _multi(s) for s in self.slots)
        self.trades_today = 0
        self.consecutive_losses = 0
        self.pnl_today = 0.0
        self.last_error: str | None = None
        self.regime: dict = {}
        self.live_quote: dict | None = (
            None  # last WS tick (ltp, bid, ask, ts) when the Dhan socket is on
        )
        # ── the trend ladder: Supertrend(14, 1.7) on 1m, 5m, 15m, 30m, 60m and 1d ──
        #: stored daily bars up to the last poll's day (`desk/history.py`); None until loaded
        self.daily_history: pd.DataFrame | None = None
        self.history_day: dt.date | None = None  # the day `daily_history` was read on
        self.history_note: str | None = None  # why there is none, when there is none
        #: the ladder at the last closed fine bar, as the card shows it ({} before any bars)
        self.trend: dict = {}
        self._ladders = ms.LadderCache()  # shared by the card and every multi-timeframe slot
        self._fine_memo: tuple[tuple, pd.DataFrame] | None = None
        self._daily_memo: tuple[tuple, pd.DataFrame, str | None] | None = None
        self._trend_failed = False

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

    # ── the trend ladder ──────────────────────────────────────────────────
    def fine_bars(self) -> pd.DataFrame:
        """The fine bars as the trend ladder reads them: without mock sessions and on one
        price scale (`data/hygiene.clean_intraday`, what the Lab loads). Nearly always the
        very frame the desk polled; kept until the bars change."""
        key = ms.fingerprint(self.bars)
        if self._fine_memo is None or self._fine_memo[0] != key:
            self._fine_memo = (key, clean_intraday(self.series.key, self.bars))
        return self._fine_memo[1]

    def daily_bars(self) -> pd.DataFrame | None:
        """The days of the ladder's daily rung: the stored history with the live days laid on
        top, on the live bars' price scale (`data/hygiene.consistent_daily`, the way the Lab
        builds them). None without stored history: the live days alone are too few for a
        daily Supertrend.

        Today's unfinished day is in it and does no harm: a day counts only from the
        following midnight (`indicators/mtf.nominal_close_ns`)."""
        history = self.daily_history
        if history is None or history.empty:
            return None
        fine = self.fine_bars()
        key = (*ms.fingerprint(history), *ms.fingerprint(fine))
        if self._daily_memo is None or self._daily_memo[0] != key:
            self._daily_memo = (key, consistent_daily(history, fine), _history_gap(history, fine))
        return self._daily_memo[1]

    def _ladder_context(self, fine: pd.DataFrame, market: pd.DataFrame | None = None) -> Context:
        """What a multi-timeframe reading needs: the fine bars, the days and the shared ladders."""
        return Context(
            symbol=self.series.key,
            daily=self.daily_bars(),
            market=market,
            extras={
                "has_volume": bool(self.series.has_volume),
                "minute_bars": fine,
                "minute_resolution": f"{self.fine_minutes}m",
                "mtf_cache": self._ladders,
            },
        )

    def _read_trend(self, at: pd.Timestamp) -> None:
        """The Supertrend(14, 1.7) ladder at the close of the last fine bar: the micro trend
        the card shows. Read at the DATA time, never the wall clock, so a late poll says what
        a backtest of that minute would. A failure leaves the card without a ladder and never
        stops a strategy."""
        try:
            fine = self.fine_bars()
            ladder = ms.ladder_for(
                fine,
                self._ladder_context(fine),
                f"{self.fine_minutes}m",
                mtf.ST_PERIOD,
                mtf.ST_MULT,
            )
            trend = ms.snapshot(ladder, at)
            trend["at"] = at.isoformat()
            trend["note"] = self._trend_note(trend)
            self.trend = trend
            self._trend_failed = False
        except Exception:  # noqa: BLE001 - the ladder is a reading, never a dependency
            if not self._trend_failed:
                log.exception("desk %s: the trend ladder failed", self.cfg.key)
            self._trend_failed = True
            self.trend = {}

    def _trend_note(self, trend: dict) -> str | None:
        """Why a timeframe has no reading, or why the daily one may be off (None: all read)."""
        notes: list[str] = []
        missing = list(trend.get("unavailable") or [])
        if missing:
            notes.append(
                f"{', '.join(missing)} cannot be built from {self.fine_minutes}-minute bars"
            )
        quiet = [r["timeframe"] for r in trend.get("rows", ()) if r["direction"] == 0]
        warming = [tf for tf in quiet if tf not in missing and tf != "1d"]
        if warming:
            notes.append(f"{', '.join(warming)} warming up ({_WARM_BARS} bars needed)")
        history = self.daily_history
        if history is None or history.empty:
            notes.append(
                self.history_note or "no stored daily history: the 1d trend has no reading"
            )
        elif self._daily_memo is not None and self._daily_memo[2]:
            notes.append(self._daily_memo[2])
        elif "1d" in quiet:
            notes.append(f"too few stored days for the 1d trend ({_WARM_BARS} needed)")
        return "; ".join(notes) or None

    # ── evaluation ────────────────────────────────────────────────────────
    def evaluate(
        self,
        now: dt.datetime,
        market_bars: pd.DataFrame | None,
        entries_allowed: bool,
        keep: Collection[tuple[str, str]] = (),
    ) -> list[tuple[StrategySlot, Verdict]]:
        """Run every slot on its closed bars; return the slots whose LAST bar fired.

        A paused slot (`enabled: false`, US-067 D9) is not evaluated and never fires, except
        that one named in `keep` (name, timeframe), a slot with a trade still active, is
        evaluated so the trade keeps its strategy exit and trail (D16); it still never fires."""
        fired: list[tuple[StrategySlot, Verdict]] = []
        if self.bars.empty:
            return fired
        latest_close_at = self.bars.index[-1] + pd.Timedelta(minutes=self.fine_minutes)
        self._read_trend(latest_close_at)
        for slot in self.slots:
            paused = not slot.cfg.enabled
            if paused and (slot.cfg.name, slot.cfg.timeframe) not in keep:
                slot.last_verdict = Verdict("none", "paused")
                slot.error = None
                continue
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
            if v.direction != "none" and v.fresh and entries_allowed and not paused:
                fired.append((slot, v))
        return fired

    def _evaluate_slot(
        self,
        slot: StrategySlot,
        latest_close_at: pd.Timestamp,
        market_bars: pd.DataFrame | None,
        now: dt.datetime,
    ) -> Verdict:
        # A multi-timeframe strategy reads what the Lab gives it: the fine bars without mock
        # sessions and on one price scale, the days, and the ladder built from them. Every
        # other strategy keeps the polled bars exactly as they are.
        multi = _multi(slot)
        fine = self.fine_bars() if multi else self.bars
        df = fine
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
        if multi:
            ctx = self._ladder_context(fine, mkt)
        else:
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
        if not multi or not self._plain_slots:
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
        # the stop of an open trade follows the strategy's own line when it gives one: the
        # level after this close (None while the line is on the other side)
        slot.line_trail = "long_trail" in f.columns or "short_trail" in f.columns
        slot.last_trail = (
            {side: _num(last.get(f"{side}_trail")) for side in ("long", "short")}
            if slot.line_trail
            else {}
        )
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
            "trend": self.trend,
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


def _multi(slot: StrategySlot) -> bool:
    """True for a strategy that reads the trend ladder: it is given the fine bars and the days."""
    return bool(getattr(slot.strategy, "multi_timeframe", False))


def _history_gap(history: pd.DataFrame, fine: pd.DataFrame) -> str | None:
    """A note when the stored days end well before the live bars begin: the sessions between
    are in neither, so the daily trend is built without them. None when the two meet."""
    if history is None or history.empty or fine is None or fine.empty:
        return None
    first_live = pd.Timestamp(fine.index[0]).date()
    older = [d for d in pd.DatetimeIndex(history.index).date if d < first_live]
    if not older:
        return None
    gap = (first_live - older[-1]).days
    if gap <= HISTORY_GAP_DAYS:
        return None
    return (
        f"stored daily history ends {older[-1]:%d %b %Y}, {gap} days before the live bars "
        "begin: the 1d trend is built without the sessions between (run a backfill)"
    )


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
