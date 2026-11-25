"""One Simulator session: the cursor, the strategy's signals, the alert, the trade and the
ratchet, minute by one-minute bar (`.agile/stories/SIM-API.md` is the contract).

Signals are the Lab's own: the strategy frame is built as `lab/runner.py:
generate_for_instrument` builds it (instrument session times, the market index's bars, the
one-minute frame in `ctx.extras["minute_bars"]` for a multi-timeframe strategy) on 120
calendar days of history before the start. A past session computes the frame once on the
bars it loaded; `tests/sim/test_no_lookahead.py` proves the signals equal those computed on
the frame cut at the cursor. A live session rebuilds it whenever new bars arrive.

A signal on a candle is an ALERT once the cursor has passed the candle's close. One thing at
a time: a signal while an alert is pending or a trade is open is dropped. A taken alert fills
at the open of the next one-minute bar; the first stop is `stop_atr_k` x ATR(14) of the
managed price (on 60m bars when the timeframe is shorter, `stop_atr_tf`), the first target
`target_r` risks away, and from then on `manage/ratchet.py` moves them minute by minute.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST, now_ist
from .. import settings as _settings
from ..config import PROJECT_ROOT, BacktestCfg
from ..costs import CostModel
from ..data.expired_options import OptionBarStore, Underlying
from ..data.hygiene import consistent_daily
from ..data.resample import minutes_of, resample_bars
from ..data.universe import InstrumentSpec, SeriesSpec, market_index_key
from ..indicators import core as ind
from ..lab.runner import _session_params
from ..manage import ratchet
from ..manage.live import atr_timeframe
from ..strategies.base import Context, Strategy
from ..strategies.registry import get_strategy
from .bars import BLOCK_DAYS, BarSource, day_start
from .frames import Frame, FrameCache, Window
from .plots import plot_values
from .pricing import Contract, LivePremium, Pricer, StoredPremiums

#: the trade plan's settings (US-074 sets them); a session's `params` may override them
#: `stop_atr_tf`: "60m" sizes the first stop on ATR(14) of 60m bars when the strategy's timeframe
#: is shorter than 60m (US-074: 15m stops lost about 1% of premium a trade), "own" on its own
DEFAULTS: dict[str, Any] = {
    "stop_atr_k": 1.5,
    "target_r": 3.0,
    "extend": True,
    "stop_atr_tf": "60m",
}
#: the session's other settings
SESSION_DEFAULTS: dict[str, Any] = {"capital": 100000.0, "auto_exit": True}
HISTORY_DAYS = 120  # calendar days of one-minute bars before the start (the 4h trend needs ~75)
#: the calendar days of history a strategy's signals need before a session's window: a
#: shorter history gives the SAME signals as 120 days (`tests/sim/test_warmup.py` proves each
#: on fixture bars); a strategy not named here keeps `HISTORY_DAYS`. The plan's ATR always
#: reads `HISTORY_DAYS` (its 60m ATR(14) converges to the last digit only after months).
WARMUP_DAYS: dict[str, int] = {
    # single-timeframe strategies: equal signals from 20 days on in the study of 25 Nov
    # (two seeds, the default and the 60m timeframe); 45 keeps more than twice that
    "ema_cross": 45,
    "supertrend_vol": 45,
    "supertrend_adx": 45,
    "orb": 45,
    "vwap_pullback": 45,
    "squeeze": 45,
    "donchian_breakout": 45,
    "mean_reversion": 45,
    "intraday_momentum": 45,
    "cpr_breakout": 45,
    "nr7_breakout": 45,
    "turtle_soup": 45,
    # the 4h trend of the seven timeframes: equal from 60 days on (not at 45)
    "microtrend": 90,
    # kept at 120: supertrend_mtf (its daily Supertrend differs even at 90 days), gap_fade and
    # ema_pullback (too few signals on the fixture bars to prove a shorter history)
}
#: a past session's window: from its block's first day (minus the history) to the end of the
#: NEXT block; the next window is prepared in the background once the cursor reaches the
#: window's last block (US-095: the reload never runs inside an `advance`)
WINDOW_BLOCKS = 2
ALERT_MINUTES = 10  # an alert lasts this many market minutes
MAX_MARKS = 400  # rows of `marks` on a read (the open trade since its entry)
MAX_EVENTS = 200  # the events a read carries
SAVE_EVERY = 30  # the cursor is saved at least every this many revealed minutes
#: the kinds that stop `advance` with `until: "alert"` (the things that need the user)
STOP_KINDS = frozenset({"ALERT", "EXIT_SIGNAL", "CLOSED", "CANCELLED", "DAY_END", "DATA_END"})
MAX_CANDLES = 300
MAX_ADVANCE = 390
ATR_PERIOD = 14
ATR_TIMEFRAMES = ("60m", "own")
TRAINING = frozenset({"MIDCPNIFTY"})  # simulated, never traded: "training only"

_NS = 1_000_000_000
_MIN_NS = 60 * _NS
_DAY_NS = 86_400 * _NS
_IST_NS = 19_800 * _NS
STAGE_NAMES = ratchet.STAGES


class SimError(Exception):
    """A request that does not fit: `status` 400, 404 or 409, with a plain sentence."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Setup:
    """What a session is, fixed at creation."""

    id: str
    instrument_key: str
    label: str
    kind: str  # INDEX | STOCK
    series_key: str
    spec: InstrumentSpec  # what is traded: the index's options, or the share
    series: SeriesSpec | None
    strategy: str
    timeframe: str
    mode: str  # past | live
    start: pd.Timestamp
    params: dict[str, Any]  # DEFAULTS and SESSION_DEFAULTS, overridden by the request
    created_at: dt.datetime = field(default_factory=now_ist)

    @property
    def tf_minutes(self) -> int:
        return minutes_of(self.timeframe)


@dataclass
class Deps:
    """What a session needs besides its bars."""

    bt: BacktestCfg
    stock_costs: CostModel
    option_costs: CostModel
    option_store: OptionBarStore | None = None
    live_premium: LivePremium | None = None
    #: (instrument key, strike, CE|PE) -> the desk's chain security id, mode live
    security_id_for: Callable[[str, float, str], str | None] | None = None
    clock: Callable[[], dt.datetime] = now_ist


def _num(v: Any, nd: int = 2) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return pd.Timestamp(int(ns), tz="UTC").tz_convert(IST).isoformat()


def _ns_of(ts: Any) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    return int(t.as_unit("ns").value)


def _hhmm_min(v: str) -> int:
    h, m = str(v).split(":")
    return int(h) * 60 + int(m)


def _local(ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(day number, minute of the day) in IST of epoch nanoseconds."""
    local = ns + _IST_NS
    return local // _DAY_NS, (local % _DAY_NS) // _MIN_NS


def end_ns(starts: np.ndarray, minutes: int, close_min: int) -> np.ndarray:
    """When candles starting at `starts` close: start + length, cut at the day's close."""
    day, _ = _local(starts)
    close = day * _DAY_NS - _IST_NS + close_min * _MIN_NS
    return np.minimum(starts + minutes * _MIN_NS, close)


def warmup_days(strategy: str) -> int:
    return int(WARMUP_DAYS.get(strategy, HISTORY_DAYS))


def window_at(strategy: str, at_ns: int) -> Window:
    """The window of a past session whose cursor is at `at_ns` (block aligned)."""
    block = int((at_ns + _IST_NS) // _DAY_NS) // BLOCK_DAYS
    first = block * BLOCK_DAYS
    w = warmup_days(strategy)
    return Window(
        day_start(first - max(w, HISTORY_DAYS)),
        day_start(first - w),
        day_start(first + WINDOW_BLOCKS * BLOCK_DAYS),
    )


def _ts_ns(ts: pd.Timestamp | None) -> int | None:
    return None if ts is None else int(ts.value)


@dataclass
class TradeState:
    """The open (or pending) trade. Managed prices in `plan`/`state`; traded in rupees."""

    id: int
    side: int
    qty: int
    action: str
    what: str
    status: str = "PENDING_FILL"
    taken_ns: int = 0
    alert: dict[str, Any] = field(default_factory=dict)
    contract: Contract | None = None
    plan: ratchet.Plan | None = None
    state: ratchet.State | None = None
    entry_ns: int | None = None
    entry_price: float | None = None
    entry_source: str | None = None
    #: the implied volatility the trade's modelled premiums use (None: the VIX-based one)
    iv: float | None = None
    iv_used: float | None = None
    iv_source: str = "vix"  # implied | vix
    risk_inr: float | None = None
    mark: float | None = None
    mark_price: float | None = None
    pending_exit: str | None = None

    def detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "taken_ns": self.taken_ns,
            "alert": self.alert,
            "contract": self.contract.as_dict() if self.contract else None,
            "entry_source": self.entry_source,
            "iv_used": self.iv_used,
            "iv_source": self.iv_source,
            "risk_inr": self.risk_inr,
            "pending_exit": self.pending_exit,
        }
        if self.plan is not None and self.state is not None:
            p, s = self.plan, self.state
            d["plan"] = {
                "side": p.side,
                "entry": p.entry,
                "stop": p.stop,
                "target": p.target,
                "cost": p.cost,
            }
            d["state"] = {"stop": s.stop, "target": s.target, "best": s.best, "stage": s.stage}
        return d


def lot_unit_of(spec: InstrumentSpec, kind: str) -> tuple[int, str]:
    """Units of one trade and their word: an index option's lot, or one share."""
    return (int(spec.lot_size), "lot") if kind == "INDEX" else (1, "share")


def chart_of(spec: InstrumentSpec, kind: str) -> dict[str, Any]:
    """`session.chart`: the managed price's step, the units of one trade and their word."""
    lot, unit = lot_unit_of(spec, kind)
    return {"tick": float(spec.tick_size), "lot": lot, "unit": unit}


def base_premium_note(setup: Setup, deps: Deps) -> str:
    if setup.kind == "STOCK":
        return "share prices from the one-minute bars, with the Lab's slippage and charges"
    if setup.mode == "live":
        return "today's traded premiums through the desk where it has them, otherwise modelled"
    if deps.option_store is None:
        return "modelled premiums (Black-76): no expired-options store was found"
    return "real traded premiums where stored, otherwise modelled"


def session_view_of(setup: Setup, cursor: str, status: str, premium_note: str) -> dict[str, Any]:
    s = setup
    return {
        "id": s.id,
        "instrument": {"key": s.instrument_key, "label": s.label, "kind": s.kind},
        "strategy": s.strategy,
        "timeframe": s.timeframe,
        "tf_minutes": s.tf_minutes,
        "mode": s.mode,
        "start": s.start.isoformat(),
        "cursor": cursor,
        "status": status,
        "capital": _num(s.params["capital"]),
        "params": {
            "stop_atr_k": float(s.params["stop_atr_k"]),
            "target_r": float(s.params["target_r"]),
            "extend": bool(s.params["extend"]),
            "auto_exit": bool(s.params["auto_exit"]),
            "stop_atr_tf": str(s.params.get("stop_atr_tf", "60m")),
        },
        "premium_note": premium_note,
        "chart": chart_of(s.spec, s.kind),
    }


_MKT_LOCK = threading.Lock()
_MKT: dict[str, Any] = {}


def market_key() -> str:
    """`data.universe.market_index_key()`, read once per change of `config/universe.yaml`
    (server review F2: the YAML load took 18 to 33 ms and ran several times per request)."""
    root = _settings._override["project_root"] or PROJECT_ROOT
    path = f"{root}/config/universe.yaml"
    try:
        st = os.stat(path)
        stamp: tuple = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (path, None, None)
    with _MKT_LOCK:
        if _MKT.get("stamp") == stamp:
            return str(_MKT["key"])
    key = market_index_key()
    with _MKT_LOCK:
        _MKT["stamp"], _MKT["key"] = stamp, key
    return key


class Frames:
    """Where a session's frames come from: built at once (any `BarSource`), or kept in a
    `FrameCache` when the bars carry a data version (`StoreBars.version`). `prefetch(setup,
    bars, window)` is called when a past session will soon need its next window; the manager
    builds it in its worker so that the swap inside `advance` finds it ready."""

    def __init__(
        self,
        cache: FrameCache | None = None,
        prefetch: Callable[[Setup, BarSource, Window], Any] | None = None,
    ) -> None:
        self.cache = cache
        self.prefetch = prefetch

    def key(self, setup: Setup, bars: BarSource, window: Window) -> tuple | None:
        version = getattr(bars, "version", None)
        if self.cache is None or version is None:
            return None
        sp = _session_params(setup.spec, {}, setup.strategy)
        params = get_strategy(setup.strategy, {**sp, "timeframe": setup.timeframe}).p
        mkt = market_key()
        series = (setup.series_key,) if mkt == setup.series_key else (setup.series_key, mkt)
        versions = tuple(version(k) for k in series)
        atr_tf = atr_timeframe(setup.timeframe, str(setup.params.get("stop_atr_tf", "60m")))
        return (
            setup.series_key,
            setup.strategy,
            setup.timeframe,
            json.dumps(params.model_dump(), sort_keys=True, default=str),
            atr_tf,
            (_ts_ns(window.lo), _ts_ns(window.lo_strategy), _ts_ns(window.hi)),
            id(build_signals),  # a test that swaps the signal builder never shares a frame
            id(bars),
            versions,
        )

    def ready(self, setup: Setup, bars: BarSource, window: Window) -> bool:
        key = self.key(setup, bars, window)
        return key is not None and self.cache is not None and self.cache.has(key)

    def frame(
        self,
        setup: Setup,
        bars: BarSource,
        window: Window,
        step: Callable[[str], None] | None = None,
    ) -> Frame:
        def build() -> Frame:
            if step is not None:
                step("reading bars")
            minute = bars.minute_bars(setup.series_key, window.lo, window.hi)
            if step is not None:
                step("building signals")
            return make_frame(setup, bars, minute, window)

        key = self.key(setup, bars, window)
        if key is None or self.cache is None:
            return build()
        self.cache.drop_stale(setup.series_key, key[-1])
        return self.cache.get(key, build)


def make_frame(
    setup: Setup, bars: BarSource, minute: pd.DataFrame, window: Window, key: tuple = ()
) -> Frame:
    """A session's frame on the one-minute bars `minute` (the Lab's way, `build_signals`)."""
    s = setup
    sp = _session_params(s.spec, {}, s.strategy)
    close_min = _hhmm_min(s.spec.session.get("close", "15:30"))
    strategy = get_strategy(s.strategy, {**sp, "timeframe": s.timeframe})
    idx = pd.DatetimeIndex(minute.index)
    n = len(idx)
    ts = idx.as_unit("ns").asi8.astype(np.int64) if n else np.zeros(0, np.int64)
    cols = {k: minute[k].to_numpy(float) if n else np.zeros(0) for k in COLUMNS}
    day, mod = _local(ts)
    empty_i = np.zeros(0, np.int64)
    if minute.empty:
        return Frame(key, window, minute, ts, cols["open"], cols["high"], cols["low"],
                     cols["close"], cols["volume"], day, mod, None, minute, empty_i, empty_i,
                     {}, np.zeros(0), empty_i, {})  # fmt: skip
    lo_s = window.lo_strategy
    sm = minute if lo_s is None or lo_s == window.lo else minute[idx >= lo_s]
    f, candles = build_signals(strategy, s.spec, s.series_key, s.timeframe, sm, bars, s.series)
    cstart = pd.DatetimeIndex(candles.index).as_unit("ns").asi8.astype(np.int64)
    cend = end_ns(cstart, s.tf_minutes, close_min)
    signals: dict[str, np.ndarray] = {}
    if f is not None:
        signals = {
            "le": f["long_entry"].to_numpy(bool),
            "se": f["short_entry"].to_numpy(bool),
            "lx": f["long_exit"].to_numpy(bool),
            "sx": f["short_exit"].to_numpy(bool),
            "lsc": np.nan_to_num(f["long_score"].to_numpy(float)),
            "ssc": np.nan_to_num(f["short_score"].to_numpy(float)),
            "fstart": pd.DatetimeIndex(f.index).as_unit("ns").asi8.astype(np.int64),
        }
    atr_tf = atr_timeframe(s.timeframe, str(s.params.get("stop_atr_tf", "60m")))
    ab = resample_bars(minute, atr_tf)
    atr = ind.atr(ab["high"], ab["low"], ab["close"], ATR_PERIOD).to_numpy(float)
    astart = pd.DatetimeIndex(ab.index).as_unit("ns").asi8.astype(np.int64)
    view = vstart = vend = None
    if sm is not minute:  # the chart's history reaches before the strategy's candles
        view = minute if s.timeframe == "1m" else resample_bars(minute, s.timeframe)
        vstart = pd.DatetimeIndex(view.index).as_unit("ns").asi8.astype(np.int64)
        vend = end_ns(vstart, s.tf_minutes, close_min)
    return Frame(
        key,
        window,
        minute,
        ts,
        cols["open"],
        cols["high"],
        cols["low"],
        cols["close"],
        cols["volume"],
        day,
        mod,
        f,
        candles,
        cstart,
        cend,
        signals,
        atr,
        end_ns(astart, minutes_of(atr_tf), close_min),
        plot_values(s.strategy, f, strategy.p, s.timeframe),
        view,
        vstart,
        vend,
    )


def make_frame_minutes(frame: Frame, minute: pd.DataFrame) -> Frame:
    """A live frame with newer minutes under the same candles and signals."""
    idx = pd.DatetimeIndex(minute.index)
    ts = idx.as_unit("ns").asi8.astype(np.int64)
    day, mod = _local(ts)
    return dataclasses.replace(
        frame,
        m=minute,
        ts=ts,
        o=minute["open"].to_numpy(float),
        h=minute["high"].to_numpy(float),
        lo=minute["low"].to_numpy(float),
        c=minute["close"].to_numpy(float),
        v=minute["volume"].to_numpy(float),
        day=day,
        mod=mod,
    )


COLUMNS = ("open", "high", "low", "close", "volume")


class SimSession:
    """A session's whole state. Not thread-safe: the manager holds one lock per session."""

    def __init__(
        self,
        setup: Setup,
        deps: Deps,
        bars: BarSource,
        store: Any,
        restore: dict | None = None,
        frames: Frames | None = None,
        step: Callable[[str], None] | None = None,
    ) -> None:
        self.setup, self.deps, self.bars, self.store = setup, deps, bars, store
        self.frames = frames or Frames()
        self._step = step
        sp = _session_params(setup.spec, {}, setup.strategy)
        self.exit_min = _hhmm_min(sp["exit_time"])
        self.close_min = _hhmm_min(setup.spec.session.get("close", "15:30"))
        self.kind = setup.kind
        self.strategy: Strategy = get_strategy(setup.strategy, {**sp, "timeframe": setup.timeframe})
        self.rparams = ratchet.RatchetParams(extend=bool(setup.params["extend"]))
        self.seq = 0
        self.alert: dict[str, Any] | None = None
        self.alert_no = 0
        self.trade: TradeState | None = None
        self.closed: list[dict[str, Any]] = []
        self.log: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.trade_marks: deque[list[float | int | None]] = deque(maxlen=MAX_MARKS)
        self.revealed = 0  # minutes revealed since the start
        self.saved_revealed = 0  # `revealed` when the cursor was last saved
        self.status = "RUNNING"
        self.day_end = False
        self.over = False
        self.next_candle_ns = 0  # the start of the first candle not yet announced
        self.hist_start = setup.start - pd.Timedelta(days=HISTORY_DAYS)
        self.window: Window | None = None
        self._prefetched: Window | None = None
        self._frame_mark: tuple[int, int] = (0, 0)
        self._frame_bucket: tuple[int, int] = (-1, -1)
        if restore is None:
            start_ns = _ns_of(setup.start)
            self._load_at(start_ns, start_ns)
            self.pos = int(np.searchsorted(self.ts, start_ns, side="left")) - 1
            if self.setup.mode == "past" and self.pos + 1 >= len(self.ts):
                raise SimError(400, "There are no stored one-minute bars after that start.")
            if self.pos < 0:
                raise SimError(400, "There are no stored one-minute bars before that start.")
            self.cursor_ns = int(self.ts[self.pos])
            self.next_candle_ns = self._first_open_candle(self.cursor_ns)
        else:
            self._restore(restore)

    # ── data ──────────────────────────────────────────────────────────────
    def _load_at(
        self, at_ns: int, after_ns: int | None = None, window: Window | None = None
    ) -> None:
        """The frame of the window that holds `at_ns` (a live session: everything from the
        history start). When that leaves nothing after `after_ns` (a gap in the store longer
        than the window), read on to the end of the stored data rather than end the session
        while later bars exist."""
        s = self.setup
        if s.mode == "live":
            win = Window(self.hist_start, self.hist_start, None)
            minute = self.bars.minute_bars(s.series_key, self.hist_start, None)
            self._install(make_frame(s, self.bars, minute, win), win)
            return
        win = window or window_at(s.strategy, at_ns)
        frame = self.frames.frame(s, self.bars, win, self._step)
        if (
            win.hi is not None
            and after_ns is not None
            and (len(frame.ts) == 0 or int(frame.ts[-1]) <= after_ns)
        ):
            rng = self.bars.minute_range(s.series_key)
            if rng is not None and _ns_of(rng[1]) > after_ns:
                win = Window(win.lo, win.lo_strategy, None)
                frame = self.frames.frame(s, self.bars, win, self._step)
        self._install(frame, win)

    def _install(self, frame: Frame, window: Window) -> None:
        """Take a frame's arrays (read only: a frame may be shared by several sessions)."""
        self.window = window
        self.frame = frame
        self.m = frame.m
        self.ts, self.o, self.h, self.lo, self.c, self.v = (
            frame.ts, frame.o, frame.h, frame.lo, frame.c, frame.v
        )  # fmt: skip
        self.day, self.mod = frame.day, frame.mod
        self.f, self.candles = frame.f, frame.candles
        self.cstart, self.cend = frame.cstart, frame.cend
        sig = frame.signals
        if sig:
            self.le, self.se, self.lx, self.sx = sig["le"], sig["se"], sig["lx"], sig["sx"]
            self.lsc, self.ssc, self.fstart = sig["lsc"], sig["ssc"], sig["fstart"]
        self.atr, self.atr_end = frame.atr, frame.atr_end
        self._frame_mark = (len(self.ts), int(self.ts[-1]) if len(self.ts) else 0)
        if self._step is not None:
            self._step("pricing")
        self.pricer = self._pricer(frame.m if len(frame.ts) else None)

    def _end_ns(self, starts: np.ndarray, minutes: int) -> np.ndarray:
        """When candles starting at `starts` close: start + length, cut at the day's close."""
        return end_ns(starts, minutes, self.close_min)

    def _pricer(self, minute: pd.DataFrame | None) -> Pricer:
        s, d = self.setup, self.deps
        if s.kind == "STOCK":
            return Pricer(s.spec, "STOCK", d.bt, d.stock_costs)
        vix = None
        self.vix_last = None
        try:
            daily = self.bars.daily_bars("INDIAVIX")
            if daily is not None and not daily.empty:
                vix = daily["close"].copy()
                vix.index = pd.DatetimeIndex(vix.index).normalize()
                self.vix_last = vix.index.max()
        except Exception:  # noqa: BLE001 - the constant IV instead
            vix = None
        stored = None
        if d.option_store is not None and minute is not None and not minute.empty:
            try:
                und = Underlying.from_specs(s.series, s.spec)
                stored = StoredPremiums(d.option_store, und, s.spec, d.bt, d.option_costs, minute)
            except (ValueError, AttributeError):
                stored = None
        exch = getattr(s.series, "exchange", "NSE") or "NSE"
        return Pricer(
            s.spec,
            "INDEX",
            d.bt,
            d.option_costs,
            vix,
            stored,
            d.live_premium if s.mode == "live" else None,
            s.series_key,
            f"{exch}_FNO",
        )

    def _first_open_candle(self, cursor_ns: int) -> int:
        """The start of the first candle not closed at the cursor (its last minute after it)."""
        i = int(np.searchsorted(self.cend, cursor_ns + _MIN_NS, side="right"))
        return int(self.cstart[i]) if i < len(self.cstart) else cursor_ns + _MIN_NS

    def _atr_at(self, t_ns: int) -> float:
        i = int(np.searchsorted(self.atr_end, t_ns, side="right")) - 1
        return float(self.atr[i]) if i >= 0 else float("nan")

    def next_window(self) -> Window | None:
        """The window a past session moves to when its cursor leaves this one."""
        w = self.window
        if self.setup.mode != "past" or w is None or w.hi is None:
            return None
        last_block = int((int(w.hi.value) + _IST_NS) // _DAY_NS) // BLOCK_DAYS - 1
        return window_at(self.setup.strategy, int(day_start(last_block * BLOCK_DAYS).value))

    def _maybe_prefetch(self) -> None:
        """Once the cursor is in the window's last block, have the next window prepared."""
        nxt = self.next_window()
        if nxt is None or nxt == self._prefetched or self.frames.prefetch is None:
            return
        w = self.window
        assert w is not None and w.hi is not None
        if self.cursor_ns + BLOCK_DAYS * _DAY_NS >= int(w.hi.value):
            self._prefetched = nxt
            job = self.frames.prefetch(self.setup, self.bars, nxt)
            if job is not None:
                # dropped (a session's build waited), replaced or failed: ask again on a later
                # advance (server review F4), so the swap never builds inside `advance`
                def retry(j: Any, nxt: Window = nxt) -> None:
                    if not j.ok and self._prefetched == nxt:
                        self._prefetched = None

                job.then(retry)

    def _more_past_data(self) -> bool:
        """Move a past session to its next window when the cursor reached the last minute
        loaded (the frame was prepared ahead: `_maybe_prefetch`)."""
        if self.setup.mode != "past" or self.window is None or self.window.hi is None:
            return False
        rng = self.bars.minute_range(self.setup.series_key)
        if rng is None or len(self.ts) == 0 or _ns_of(rng[1]) <= int(self.ts[-1]):
            return False
        cursor = self.cursor_ns
        self._load_at(cursor + _MIN_NS, cursor, self.next_window())
        self.pos = int(np.searchsorted(self.ts, cursor, side="right")) - 1
        return self.pos + 1 < len(self.ts)

    def _bucket(self, ns: int) -> tuple[int, int]:
        """(day, candle number) of the candle an instant falls in, bins from 09:15."""
        day, mod = _local(np.array([ns], np.int64))
        return int(day[0]), (int(mod[0]) - 555) // self.setup.tf_minutes

    def _refresh_live(self) -> None:
        """The bars that have arrived; the frame is rebuilt only when a candle has closed
        since the last build (the signals are causal: tests/sim/test_no_lookahead.py)."""
        minute = self.bars.minute_bars(self.setup.series_key, self.hist_start, None)
        if minute.empty:
            return
        idx = pd.DatetimeIndex(minute.index).as_unit("ns").asi8
        if (len(idx), int(idx[-1])) == self._frame_mark:
            return
        last = int(idx[-1])
        bucket = self._bucket(last + _MIN_NS)
        win = Window(self.hist_start, self.hist_start, None)
        if self.f is None or bucket != self._frame_bucket:
            self._install(make_frame(self.setup, self.bars, minute, win), win)
            self._frame_bucket = bucket
        else:
            # the same candles: only the minutes are new (the frame's signals stand)
            frame = make_frame_minutes(self.frame, minute)
            self.frame = frame
            self.m, self.ts, self.o, self.h, self.lo, self.c, self.v = (
                frame.m, frame.ts, frame.o, frame.h, frame.lo, frame.c, frame.v
            )  # fmt: skip
            self.day, self.mod = frame.day, frame.mod
            self._frame_mark = (len(self.ts), last)
        self.pos = int(np.searchsorted(self.ts, self.cursor_ns, side="right")) - 1

    # ── views ─────────────────────────────────────────────────────────────
    @property
    def cursor(self) -> str:
        return str(_iso(self.cursor_ns))

    def _view_arrays(self) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
        f = self.frame
        if f.view is not None and f.vstart is not None and f.vend is not None:
            return f.view, f.vstart, f.vend
        return self.candles, self.cstart, self.cend

    def candles_view(self) -> list[list[float | int | None]]:
        """Up to 300 closed candles that end at or before the cursor, oldest first."""
        candles, cstart, cend = self._view_arrays()
        n = int(np.searchsorted(cend, self.cursor_ns + _MIN_NS, side="right"))
        lo = max(0, n - MAX_CANDLES)
        cd = candles.iloc[lo:n]
        return [
            [int(t // _NS), _num(o), _num(h), _num(low), _num(c), _num(v, 0)]
            for t, o, h, low, c, v in zip(
                cstart[lo:n],
                cd["open"].to_numpy(float),
                cd["high"].to_numpy(float),
                cd["low"].to_numpy(float),
                cd["close"].to_numpy(float),
                cd["volume"].to_numpy(float),
                strict=True,
            )
        ]

    def _overlay_points(self, lo_ns: int, hi_ns: int) -> dict[str, list[list[float | int]]]:
        """For each plot, [candle start, value] of the strategy candles that closed after
        `lo_ns` and at or before `hi_ns` (a candle closes at its end); undefined values left out."""
        plots = self.frame.plots
        if not plots:
            return {}
        i = int(np.searchsorted(self.cend, lo_ns, side="right"))
        j = int(np.searchsorted(self.cend, hi_ns, side="right"))
        out: dict[str, list[list[float | int]]] = {}
        starts = self.cstart[i:j] // _NS
        for pid, vals in plots.items():
            seg = vals[i:j]
            ok = np.isfinite(seg)
            out[pid] = [
                [int(t), float(round(float(v), 2))]
                for t, v in zip(starts[ok], seg[ok], strict=True)
            ]
        return out

    def overlays_view(self) -> dict[str, list[list[float | int]]]:
        """The plots' points for the candles of `candles_view` (closed candles to the cursor)."""
        _, cstart, cend = self._view_arrays()
        n = int(np.searchsorted(cend, self.cursor_ns + _MIN_NS, side="right"))
        if n == 0:
            return {k: [] for k in self.frame.plots}
        first = int(cstart[max(0, n - MAX_CANDLES)])
        lo = int(np.searchsorted(self.cstart, first, side="left"))
        lo_ns = int(self.cend[lo - 1]) if lo > 0 else -1
        return self._overlay_points(lo_ns, self.cursor_ns + _MIN_NS)

    def _mark_row(self, p: int) -> list[float | int | None]:
        t = self.trade
        assert t is not None and t.entry_price is not None and t.mark_price is not None
        pnl = self.pricer.gross(t.side, t.entry_price, t.mark_price)
        return [int(self.ts[p] // _NS), _num(t.mark), _num(t.mark_price), _num(pnl)]

    def marks_view(self) -> list[list[float | int | None]]:
        """The open trade's marks since its entry (at most `MAX_MARKS`); empty when flat."""
        t = self.trade
        if t is None or t.status != "OPEN":
            return []
        return list(self.trade_marks)

    def _rebuild_marks(self) -> None:
        """The open trade's marks from its entry to the cursor (after a rebuild)."""
        self.trade_marks.clear()
        t = self.trade
        if t is None or t.status != "OPEN" or t.entry_ns is None or t.entry_price is None:
            return
        i = int(np.searchsorted(self.ts, t.entry_ns, side="left"))
        i = max(i, self.pos - MAX_MARKS + 1)
        keep = (t.mark, t.mark_price)
        for p in range(max(i, 0), self.pos + 1):
            t.mark, t.mark_price = float(self.c[p]), self._mark_price(t, p)
            self.trade_marks.append(self._mark_row(p))
        t.mark, t.mark_price = keep

    def vix_note(self) -> str:
        """Plain words when the volatility the model needs is missing at the cursor."""
        if self.kind == "STOCK":
            return ""
        last = getattr(self, "vix_last", None)
        if last is None:
            return " India VIX is not stored, so modelled premiums use a fixed volatility."
        day = pd.Timestamp(self.cursor_ns, tz="UTC").tz_convert(IST).tz_localize(None).normalize()
        if day > pd.Timestamp(last).tz_localize(None).normalize():
            return (
                f" India VIX is stored only to {pd.Timestamp(last):%d %b %Y}: from there modelled "
                "premiums use that last value."
            )
        return ""

    def premium_note(self) -> str:
        note = base_premium_note(self.setup, self.deps)
        if self.kind == "STOCK":
            return note
        return note + "." + self.vix_note() if self.vix_note() else note

    def session_view(self) -> dict[str, Any]:
        return session_view_of(self.setup, self.cursor, self.status, self.premium_note())

    def lot_unit(self) -> tuple[int, str]:
        return lot_unit_of(self.setup.spec, self.kind)

    def chart_view(self) -> dict[str, Any]:
        return chart_of(self.setup.spec, self.kind)

    def alert_view(self) -> dict[str, Any] | None:
        if self.alert is None:
            return None
        out = {k: v for k, v in self.alert.items() if not k.startswith("_")}
        lot, unit = self.lot_unit()
        left = ALERT_MINUTES - (self.revealed - int(self.alert.get("_rev") or 0))
        out.update(lot=lot, unit=unit, expires_in_min=max(0, int(left)))
        return out

    def trade_view(self, t: TradeState | None = None) -> dict[str, Any] | None:
        t = t or self.trade
        if t is None:
            return None
        return self._trade_row(t)

    def summary(self) -> dict[str, Any]:
        return summary_of(self.closed, float(self.setup.params["capital"]))

    def state(self) -> dict[str, Any]:
        return {
            "session": self.session_view(),
            "alert": self.alert_view(),
            "trade": self.trade_view(),
            "summary": self.summary(),
            "trades": list(self.closed),
        }

    def read(self) -> dict[str, Any]:
        """GET /api/sim/sessions/{id}: the state, the candles to the cursor, the last events,
        the plots' points of those candles and the open trade's marks."""
        return {
            **self.state(),
            "candles": self.candles_view(),
            "events": list(self.log),
            "overlays": self.overlays_view(),
            "marks": self.marks_view(),
        }

    def last_price(self) -> float | None:
        return _num(self.c[self.pos]) if 0 <= self.pos < len(self.c) else None

    # ── events ────────────────────────────────────────────────────────────
    def _event(
        self, out: list[dict], ns: int, kind: str, text: str, data: dict | None = None
    ) -> None:
        self.seq += 1
        e = {
            "seq": self.seq,
            "ts": _iso(ns),
            "kind": kind,
            "text": text,
            "data": data or {},
            "_ts": pd.Timestamp(ns, tz="UTC").to_pydatetime(),
        }
        out.append(e)
        self.log.append(self._public(e))

    # ── the actions ───────────────────────────────────────────────────────
    def _too_late(self) -> bool:
        if self.day_end or self.status == "ENDED":
            return True
        _, mod = _local(np.array([self.cursor_ns], np.int64))
        return int(mod[0]) + 1 >= self.exit_min

    def take(self, alert_id: str) -> dict[str, Any]:
        if self.status == "ENDED":
            raise SimError(409, "The session has ended.")
        if self.trade is not None:
            raise SimError(409, "A trade is already open in this session.")
        if self.alert is None or self.alert["id"] != alert_id:
            raise SimError(409, "There is no such alert to take: it expired or was skipped.")
        if self._too_late():
            raise SimError(409, "It is too late in the trading day to open a trade.")
        a = self.alert
        side = 1 if a["side"] == "LONG" else -1
        contract = Contract.from_dict(a["_contract"]) if a.get("_contract") else None
        row = {
            "session_id": self.setup.id,
            "status": "PENDING_FILL",
            "side": a["side"],
            "action": a["action"],
            "qty": int(a["qty"]),
            "what": a["traded"]["what"],
            "updated_at": now_ist(),
        }
        tid = self.store.add_trade(row)
        self.trade = TradeState(
            tid,
            side,
            int(a["qty"]),
            a["action"],
            a["traded"]["what"],
            "PENDING_FILL",
            self.cursor_ns,
            {k: v for k, v in a.items() if k != "_contract"},
            contract,
        )
        self.alert = None
        self._save([], force=True)
        return self._answer([], [])

    def skip(self, alert_id: str) -> dict[str, Any]:
        if self.alert is None or self.alert["id"] != alert_id:
            raise SimError(409, "There is no such alert to skip: it expired or was taken.")
        self.alert = None
        self._save([], force=True)
        return self._answer([], [])

    def close(self) -> dict[str, Any]:
        t = self.trade
        if t is None:
            raise SimError(409, "There is no open trade to close.")
        events: list[dict] = []
        if t.status == "PENDING_FILL":
            self._cancel(events, self.cursor_ns + _MIN_NS, "You closed the trade before it filled")
        else:
            t.pending_exit = "MANUAL"
        self._save(events, force=True)
        return self._answer(events, [])

    def end(self) -> dict[str, Any]:
        """Close an open trade at the next minute's open (the last close when there is no
        next minute) and mark the session ENDED."""
        shown: list[dict] = []
        minutes: list[list] = []
        marks: list[list] = []
        events: list[dict] = []
        t = self.trade
        if t is not None and t.status == "PENDING_FILL":
            self._cancel(
                events, self.cursor_ns + _MIN_NS, "The session ended before the trade filled"
            )
        elif t is not None and self.status != "ENDED":
            t.pending_exit = "MANUAL"
            if self.setup.mode == "past" and self.pos + 1 < len(self.ts):
                got = self.advance(1)  # saved by advance
                shown, minutes = list(got["events"]), list(got["minutes"])
                marks = list(got["marks"])
            if self.trade is not None and self.pos >= 0:  # no next minute: the last close
                p = self.pos
                end_ns = int(self.ts[p]) + _MIN_NS
                self._close_trade(events, p, "MANUAL", self.c[p], "close", end_ns)
        self.alert = None
        self.status = "ENDED"
        self._save(events, force=True)
        return self._answer(shown + [self._public(e) for e in events], minutes, marks)

    def _answer(
        self, events: list[dict], minutes: list[list], marks: list[list] | None = None
    ) -> dict[str, Any]:
        """What take, skip, close and end answer: GET's body without `candles`, plus the
        events the action produced (as in `advance`), the minutes it revealed and their
        `marks`."""
        pub = [self._public(e) if "_ts" in e else e for e in events]
        return {**self.state(), "events": pub, "minutes": minutes, "marks": marks or []}

    def _cancel(self, events: list[dict], ns: int, text: str) -> None:
        """A trade that never filled: stored as CANCELLED (never a result), and said so."""
        t = self.trade
        if t is None:
            return
        self.store.save_trade(t.id, {"status": "CANCELLED", "updated_at": now_ist()})
        self.trade = None
        self._event(events, ns, "CANCELLED", text, {"trade_id": t.id})

    # ── the clock: reveal minutes ─────────────────────────────────────────
    def advance(self, minutes: int = 1, until: str | None = None) -> dict[str, Any]:
        """Reveal up to `minutes` one-minute bars. `until`: None reveals them all; "event"
        stops after the first minute that produced any event, "alert" after the first that
        produced one of `STOP_KINDS` (SIM-API-2.md)."""
        if self.status == "ENDED":
            raise SimError(409, "The session has ended.")
        if until not in (None, "event", "alert"):
            raise SimError(400, 'until must be null, "event" or "alert".')
        events: list[dict] = []
        revealed: list[list] = []
        marks: list[list] = []
        before_ns = self.cursor_ns
        self.day_end = False
        if self.status == "DAY_END":
            self.status = "RUNNING"
        if self.setup.mode == "live":
            self._refresh_live()
            todo = MAX_ADVANCE
        else:
            todo = max(1, min(int(minutes), MAX_ADVANCE))
        while todo > 0:
            if self.pos + 1 >= len(self.ts) and not self._more_past_data():
                if self.setup.mode == "past":
                    self._data_end(events)
                break
            self.pos += 1
            p = self.pos
            self.cursor_ns = int(self.ts[p])
            self.revealed += 1
            todo -= 1
            revealed.append(
                [
                    int(self.ts[p] // _NS),
                    _num(self.o[p]),
                    _num(self.h[p]),
                    _num(self.lo[p]),
                    _num(self.c[p]),
                    _num(self.v[p], 0),
                ]
            )
            first = len(events)
            self._minute(p, events)
            over = self._is_day_end(p)
            if over:
                self._day_end(p, events)
            t = self.trade
            if t is not None and t.status == "OPEN" and t.mark_price is not None:
                row = self._mark_row(p)
                marks.append(row)
                self.trade_marks.append(row)
            if over:
                break
            new = events[first:]
            if until == "event" and new:
                break
            if until == "alert" and any(e["kind"] in STOP_KINDS for e in new):
                break
        if self.setup.mode == "past" and not self.over and self.pos + 1 >= len(self.ts):
            if not self._more_past_data():
                self._data_end(events)
        if self.setup.mode == "past":
            self._maybe_prefetch()
        self._save(events)
        return {
            "cursor": self.cursor,
            "minutes": revealed,
            "events": [self._public(e) for e in events],
            "alert": self.alert_view(),
            "trade": self.trade_view(),
            "summary": self.summary(),
            "last_price": self.last_price(),
            "over": self.over,
            "day_end": self.day_end,
            "marks": marks,
            "overlays": self._overlay_points(before_ns + _MIN_NS, self.cursor_ns + _MIN_NS),
        }

    @staticmethod
    def _public(e: dict) -> dict:
        return {k: v for k, v in e.items() if not k.startswith("_")}

    def _is_day_end(self, p: int) -> bool:
        if int(self.mod[p]) + 1 >= self.close_min:
            return True
        if self.setup.mode == "past" and p + 1 < len(self.ts):
            return bool(self.day[p + 1] != self.day[p])
        return False

    def _minute(self, p: int, events: list[dict]) -> None:
        """Everything one revealed minute does, in order: candles that ended before it, the
        fill, the exits and the ratchet, the alert's clock, then candles closing with it."""
        t0 = int(self.ts[p])
        self._close_candles(t0, p, events)
        t = self.trade
        if t is not None and t.status == "PENDING_FILL":
            self._fill(p, events)
        t = self.trade
        if t is not None and t.status == "OPEN":
            self._manage(p, events)
        if self.alert is not None and self.revealed - int(self.alert["_rev"]) >= ALERT_MINUTES:
            self._expire(events, t0 + _MIN_NS)
        self._close_candles(t0 + _MIN_NS, p, events)
        t = self.trade
        if t is not None and t.status == "OPEN":
            t.mark = float(self.c[p])
            t.mark_price = self._mark_price(t, p)

    def _close_candles(self, limit_ns: int, p: int, events: list[dict]) -> None:
        """Announce every candle that has closed by `limit_ns` and was not announced yet."""
        if self.f is None:
            return
        i = int(np.searchsorted(self.fstart, self.next_candle_ns, side="left"))
        while i < len(self.fstart) and int(self.cend[i]) <= limit_ns:
            self._on_candle(i, p, events)
            i += 1
        if i < len(self.fstart):
            self.next_candle_ns = int(self.fstart[i])
        elif i > 0:  # every candle so far announced: the next one starts at or after this end
            self.next_candle_ns = int(self.cend[i - 1])

    def _on_candle(self, i: int, p: int, events: list[dict]) -> None:
        end = int(self.cend[i])
        t = self.trade
        if t is not None and t.status == "OPEN":
            exit_sig = self.lx[i] if t.side == 1 else self.sx[i]
            if exit_sig and t.pending_exit is None:
                auto = bool(self.setup.params["auto_exit"])
                text = "The strategy signals an exit" + (
                    ": the trade closes at the next minute's open" if auto else ""
                )
                self._event(events, end, "EXIT_SIGNAL", text, {"trade_id": t.id})
                if auto:
                    t.pending_exit = "SIGNAL_EXIT"
            return
        if t is not None or self.alert is not None or self.status == "ENDED":
            return  # one thing at a time: the signal is dropped
        le, se = bool(self.le[i]), bool(self.se[i])
        if not (le or se):
            return
        if le and se:
            side = 1 if self.lsc[i] >= self.ssc[i] else -1
        else:
            side = 1 if le else -1
        _, mod = _local(np.array([end], np.int64))
        if int(mod[0]) >= self.exit_min:
            return  # too late to take: the session's exit time has come
        self._make_alert(i, p, side, end, events)

    def _make_alert(self, i: int, p: int, side: int, end: int, events: list[dict]) -> None:
        s = self.setup
        prm = s.params
        close = float(self.candles["close"].iloc[i])
        risk = float(prm["stop_atr_k"]) * self._atr_at(end)
        if not (risk > 0 and math.isfinite(risk)):
            return  # no ATR yet (too little history): nothing to plan a trade on
        tr = float(prm["target_r"])
        stop, target = close - side * risk, close + side * tr * risk
        when = pd.Timestamp(end, tz="UTC").tz_convert(IST)
        pr = self.pricer
        self.alert_no += 1
        reasons: list[str] = []
        try:
            reasons = list(
                self.strategy.explain(self.f, i, "long" if side == 1 else "short")["reasons"]
            )  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - an alert without its reasons is still an alert
            reasons = []
        contract = None
        if pr.is_option:
            opt = "CE" if side == 1 else "PE"
            sid = None
            if self.deps.security_id_for is not None and s.mode == "live":
                try:
                    probe = pr.contract_for(opt, close, when)
                    sid = self.deps.security_id_for(s.instrument_key, probe.strike, opt)
                except Exception:  # noqa: BLE001 - the model prices it
                    sid = None
            contract = pr.contract_for(opt, close, when, sid)
            bar = (float(self.o[p]), float(self.h[p]), float(self.lo[p]), float(self.c[p]))
            q = pr.premium(contract, pd.Timestamp(int(self.ts[p]), tz="UTC"), bar, close, "close")
            price = pr.option_entry(q.price)
            # a REAL premium calibrates the model: the stop and target premiums lie on the
            # curve that passes through it (else they would be VIX-model prices next to a real one)
            iv = pr.implied_iv(contract, q.price, close, when) if q.source == "real" else None
            iv_source = "implied" if iv is not None else "vix"
            stop_price = pr.option_exit(pr.model_premium(contract, stop, when, iv))
            target_price = pr.option_exit(pr.model_premium(contract, target, when, iv))
            source = q.source
            what = contract.what
            action = "BUY CALL" if side == 1 else "BUY PUT"
            risk_inr = (price - stop_price) * pr.qty
            reward_inr = (target_price - price) * pr.qty
        else:
            price = pr.stock_entry(side, close)
            stop_price, target_price, source = stop, target, "real"
            what = s.label
            action = "BUY" if side == 1 else "SELL"
            risk_inr = risk * pr.qty
            reward_inr = tr * risk * pr.qty
            iv, iv_source = None, ""
        costs = pr.charges(side, price, target_price)
        self.alert = {
            "id": f"a_{self.alert_no}",
            "ts": _iso(end),
            "expires": _iso(end + ALERT_MINUTES * _MIN_NS),
            "side": "LONG" if side == 1 else "SHORT",
            "action": action,
            "entry": _num(close),
            "stop": _num(stop),
            "target": _num(target),
            "risk": _num(risk),
            "reward": _num(tr * risk),
            "qty": pr.qty,
            "traded": {
                "what": what,
                "price": _num(price),
                "source": source,
                "stop_price": _num(stop_price),
                "target_price": _num(target_price),
                **(
                    {"iv_source": iv_source, "iv_used": _num(iv, 4)}
                    if pr.is_option and iv_source == "implied"
                    else {}
                ),
            },
            "risk_inr": _num(risk_inr),
            "reward_inr": _num(reward_inr),
            "costs_inr": _num(costs),
            "reasons": reasons,
            "_rev": self.revealed,
            "_iv": iv,
            "_contract": contract.as_dict() if contract else None,
        }
        verb = {
            "BUY CALL": "Buy a CALL",
            "BUY PUT": "Buy a PUT",
            "BUY": "Buy the share",
            "SELL": "Sell the share short",
        }[action]
        text = f"{verb}: {what} near {_num(price)}"
        data = {"alert_id": self.alert["id"], "side": self.alert["side"], "action": action}
        self._event(events, end, "ALERT", text, data)

    def _expire(self, events: list[dict], ns: int) -> None:
        a = self.alert
        if a is None:
            return
        self._event(events, ns, "ALERT_EXPIRED", "The alert expired", {"alert_id": a["id"]})
        self.alert = None

    # ── the trade ─────────────────────────────────────────────────────────
    def _fill(self, p: int, events: list[dict]) -> None:
        t = self.trade
        assert t is not None
        s, pr = self.setup, self.pricer
        t0 = int(self.ts[p])
        same_day = int(_local(np.array([t.taken_ns], np.int64))[0][0]) == int(self.day[p])
        if not same_day or int(self.mod[p]) >= self.exit_min:
            # the trading day ended before the fill: nothing traded
            self._cancel(events, t0, "The trading day ended before the trade filled")
            return
        side = t.side
        o = float(self.o[p])
        minute = pd.Timestamp(t0, tz="UTC")
        if pr.is_option:
            assert t.contract is not None
            bar = (o, float(self.h[p]), float(self.lo[p]), float(self.c[p]))
            q = pr.premium(t.contract, minute, bar, o, "open")
            entry_price = pr.option_entry(q.price)
            entry = o
            source = q.source
            # re-anchor: the fill's real premium recalibrates the model; with none, the alert's
            # implied volatility (one minute older, same curve) stays, else the VIX-based one
            iv = pr.implied_iv(t.contract, q.price, o, minute) if q.source == "real" else None
            if iv is None and t.alert.get("_iv"):
                iv = float(t.alert["_iv"])
            t.iv, t.iv_source = iv, "implied" if iv is not None else "vix"
            t.iv_used = iv if iv is not None else pr.vix_iv(minute)
            delta = max(pr.delta(t.contract, o, minute, iv), 0.05)
            cost = pr.charges(1, entry_price, entry_price) / pr.qty / delta
        else:
            entry_price = pr.stock_entry(side, o)
            entry = entry_price
            source = "real"
            cost = pr.charges(side, entry_price, entry_price) / pr.qty
        risk = float(s.params["stop_atr_k"]) * self._atr_at(t0)
        if not (risk > 0 and math.isfinite(risk)):
            risk = float(t.alert.get("risk") or 0.0) or 0.0035 * entry
        plan = ratchet.plan_from_risk(side, entry, risk, float(s.params["target_r"]), cost)
        t.plan, t.state = plan, ratchet.start(plan)
        t.status, t.entry_ns = "OPEN", t0
        t.entry_price, t.entry_source = float(entry_price), source
        if pr.is_option:
            assert t.contract is not None
            stop_fill = pr.option_exit(pr.model_premium(t.contract, plan.stop, minute, t.iv))
            t.risk_inr = max(entry_price - stop_fill, pr.tick) * pr.qty
        else:
            t.risk_inr = plan.risk * pr.qty
        t.mark, t.mark_price = entry, entry_price
        self.trade_marks.clear()
        self._event(
            events,
            t0,
            "FILLED",
            f"Filled: {t.action.lower()} {t.what} at {_num(entry_price)}",
            {
                "trade_id": t.id,
                "entry": _num(entry),
                "entry_price": _num(entry_price),
                "stop": _num(plan.stop),
                "target": _num(plan.target),
                "source": source,
            },
        )

    def _manage(self, p: int, events: list[dict]) -> None:
        t = self.trade
        assert t is not None and t.plan is not None and t.state is not None
        t0 = int(self.ts[p])
        o, h, low = float(self.o[p]), float(self.h[p]), float(self.lo[p])
        if t.pending_exit is not None:
            self._close_trade(events, p, t.pending_exit, o, "open", t0)
            return
        if int(self.mod[p]) >= self.exit_min:
            self._close_trade(events, p, "SESSION_END", o, "open", t0)
            return
        hit = ratchet.exit_on_bar(t.plan, t.state, o, h, low)
        if hit is not None:
            reason, level = hit
            gap = reason.startswith("GAP_")
            ns = t0 if gap else t0 + _MIN_NS  # a gap is known at the open, a touch at the close
            self._close_trade(events, p, reason, level, "open" if gap else "touch", ns)
            return
        before = t.state
        t.state, changes = ratchet.advance(t.plan, before, h, low, self.rparams)
        for ch in changes:
            if ch in ("BREAKEVEN", "LOCKED", "TRAILING"):
                text = {
                    "BREAKEVEN": "The stop moves above breakeven",
                    "LOCKED": "The stop locks part of the planned move",
                    "TRAILING": "The stop trails the best price",
                }[ch]
                self._event(events, t0 + _MIN_NS, "STAGE", text, {"stage": ch})
            elif ch == "STOP_MOVED":
                self._event(
                    events,
                    t0 + _MIN_NS,
                    "STOP_MOVED",
                    f"Stop moved to {_num(t.state.stop)}",
                    {"stop": _num(t.state.stop)},
                )
            elif ch == "TARGET_MOVED":
                self._event(
                    events,
                    t0 + _MIN_NS,
                    "TARGET_MOVED",
                    f"Target moved to {_num(t.state.target)}",
                    {"target": _num(t.state.target)},
                )

    def _mark_price(self, t: TradeState, p: int) -> float:
        pr = self.pricer
        if not pr.is_option or t.contract is None:
            return float(self.c[p])
        bar = (float(self.o[p]), float(self.h[p]), float(self.lo[p]), float(self.c[p]))
        q = pr.premium(
            t.contract, pd.Timestamp(int(self.ts[p]), tz="UTC"), bar, bar[3], "close", t.iv
        )
        return float(q.price)

    def _close_trade(
        self, events: list[dict], p: int, reason: str, level: float, how: str, ns: int
    ) -> None:
        t = self.trade
        assert t is not None and t.plan is not None and t.state is not None
        assert t.entry_price is not None
        pr = self.pricer
        minute = pd.Timestamp(int(self.ts[p]), tz="UTC")
        level = float(level)
        if pr.is_option:
            assert t.contract is not None
            bar = (float(self.o[p]), float(self.h[p]), float(self.lo[p]), float(self.c[p]))
            q = pr.premium(t.contract, minute, bar, level, how, t.iv)
            exit_price = pr.option_exit(q.price)
            exit_level = level
            exit_source = q.source
        else:
            slipped = reason not in ("TARGET", "GAP_TARGET")
            exit_price = pr.stock_exit(t.side, level, slipped)
            exit_level = exit_price
            exit_source = "real"
        gross = pr.gross(t.side, t.entry_price, exit_price)
        charges = pr.charges(t.side, t.entry_price, exit_price)
        net = round(gross, 2) - round(charges, 2)
        risk_inr = t.risk_inr or 0.0
        st = t.state
        detail = t.detail()
        detail["exit_source"] = exit_source
        stop_price, target_price = self._traded_levels(t, int(minute.value) + _MIN_NS)
        lot, unit = self.lot_unit()
        detail["closing"] = {
            "lot": lot,
            "unit": unit,
            "stop_price": _num(stop_price),
            "target_price": _num(target_price),
            "minutes_in_trade": self._minutes_between(t.entry_ns, int(self.ts[p])),
        }
        if t.entry_source != exit_source:
            detail["note"] = (
                f"the entry premium was {t.entry_source} and the exit premium {exit_source}"
            )
        fields = {
            "status": "CLOSED",
            "source": t.entry_source,
            "entry_ts": pd.Timestamp(t.entry_ns, tz="UTC").to_pydatetime(),
            "entry": _num(t.plan.entry),
            "entry_price": _num(t.entry_price),
            "stop": _num(st.stop),
            "target": _num(st.target),
            "stop0": _num(t.plan.stop),
            "target0": _num(t.plan.target),
            "stage": STAGE_NAMES[st.stage],
            "best": _num(st.best),
            "progress": _num(ratchet.progress(t.plan, st.best), 4),
            "mark": _num(exit_level),
            "mark_price": _num(exit_price),
            "open_pnl": None,
            "exit_ts": pd.Timestamp(ns, tz="UTC").to_pydatetime(),
            "exit": _num(exit_level),
            "exit_price": _num(exit_price),
            "exit_reason": reason,
            "gross": _num(gross),
            "charges": _num(charges),
            "net": _num(net),
            "r_multiple": _num(net / risk_inr, 4) if risk_inr > 0 else None,
            "detail": detail,
            "updated_at": now_ist(),
        }
        self.store.save_trade(t.id, fields)
        row = self._row_from_fields(t, fields)
        self.closed.insert(0, row)
        self.trade = None
        self._event(
            events,
            ns,
            "CLOSED",
            f"Closed ({reason.replace('_', ' ').lower()}) at {_num(exit_price)}: "
            f"net {_num(net)} rupees",
            {
                "trade_id": t.id,
                "exit": _num(exit_level),
                "exit_price": _num(exit_price),
                "exit_reason": reason,
                "net": _num(net),
            },
        )

    def _row_from_fields(self, t: TradeState, f: dict[str, Any]) -> dict[str, Any]:
        out = {k: f.get(k) for k in _TRADE_KEYS}
        out["id"] = t.id
        out["side"] = "LONG" if t.side == 1 else "SHORT"
        out["action"], out["qty"] = t.action, t.qty
        out["entry_ts"] = _iso(t.entry_ns)
        out["exit_ts"] = pd.Timestamp(f["exit_ts"]).tz_convert(IST).isoformat()
        traded: dict[str, Any] = {"what": t.what, "source": t.entry_source}
        if f.get("detail", {}).get("note"):
            traded["note"] = f["detail"]["note"]
        out["traded"] = traded
        out.update(_closed_extra(f.get("detail") or {}))
        return out

    def _trade_row(self, t: TradeState) -> dict[str, Any]:
        """The contract's trade shape for a pending or open trade."""
        p, st = t.plan, t.state
        open_pnl = None
        if t.status == "OPEN" and t.mark_price is not None and t.entry_price is not None:
            open_pnl = _num(self.pricer.gross(t.side, t.entry_price, t.mark_price))
        return {
            "id": t.id,
            "status": t.status,
            "side": "LONG" if t.side == 1 else "SHORT",
            "action": t.action,
            "qty": t.qty,
            "entry_ts": _iso(t.entry_ns),
            "entry": _num(p.entry) if p else None,
            "entry_price": _num(t.entry_price),
            "source": t.entry_source,
            "stop": _num(st.stop) if st else None,
            "target": _num(st.target) if st else None,
            "stop0": _num(p.stop) if p else None,
            "target0": _num(p.target) if p else None,
            "stage": STAGE_NAMES[st.stage] if st else STAGE_NAMES[0],
            "best": _num(st.best) if st else None,
            "progress": _num(ratchet.progress(p, st.best), 4) if p and st else None,
            "mark": _num(t.mark),
            "mark_price": _num(t.mark_price),
            "open_pnl": open_pnl,
            "exit_ts": None,
            "exit": None,
            "exit_price": None,
            "exit_reason": None,
            "gross": None,
            "charges": None,
            "net": None,
            "r_multiple": None,
            "traded": {"what": t.what, "source": t.entry_source},
            **self._trade_extra(t, open_pnl),
        }

    def _traded_levels(self, t: TradeState, at_ns: int) -> tuple[float | None, float | None]:
        """The traded-price equivalents of the trade's present stop and target: for an option
        by the trade's own curve at `at_ns` (SIM-API "One curve per trade"), the levels
        themselves for a stock."""
        st = t.state
        if st is None:
            return None, None
        pr = self.pricer
        if not pr.is_option or t.contract is None:
            return float(st.stop), float(st.target)
        when = pd.Timestamp(int(at_ns), tz="UTC").tz_convert(IST)
        stop = pr.option_exit(pr.model_premium(t.contract, st.stop, when, t.iv))
        target = pr.option_exit(pr.model_premium(t.contract, st.target, when, t.iv))
        return float(stop), float(target)

    def _minutes_between(self, a_ns: int | None, b_ns: int) -> int:
        """Stored minutes from the one starting at `a_ns` to the one starting at `b_ns`."""
        if a_ns is None:
            return 0
        i = int(np.searchsorted(self.ts, a_ns, side="left"))
        j = int(np.searchsorted(self.ts, b_ns, side="right"))
        return max(0, j - i)

    def _trade_extra(self, t: TradeState, open_pnl: float | None) -> dict[str, Any]:
        lot, unit = self.lot_unit()
        stop_price, target_price = (None, None)
        if t.status == "OPEN":
            stop_price, target_price = self._traded_levels(t, self.cursor_ns + _MIN_NS)
        risk = t.risk_inr
        return {
            "lot": lot,
            "unit": unit,
            "stop_price": _num(stop_price),
            "target_price": _num(target_price),
            "risk_inr": _num(risk),
            "open_r": _num(open_pnl / risk, 4) if open_pnl is not None and risk else None,
            "minutes_in_trade": self._minutes_between(t.entry_ns, self.cursor_ns),
        }

    # ── the day and the data ──────────────────────────────────────────────
    def _day_end(self, p: int, events: list[dict]) -> None:
        end_ns = int(self.ts[p]) + _MIN_NS
        self._close_candles(end_ns + self.setup.tf_minutes * _MIN_NS, p, events)
        if self.alert is not None:
            self._expire(events, end_ns)
        t = self.trade
        if t is not None and t.status == "PENDING_FILL":
            self._cancel(events, end_ns, "The trading day ended before the trade filled")
        elif t is not None:
            self._close_trade(events, p, "SESSION_END", self.c[p], "close", end_ns)
        self._event(events, end_ns, "DAY_END", "The trading day is over")
        self.day_end = True
        self.status = "DAY_END"

    def _data_end(self, events: list[dict]) -> None:
        if self.over:
            return
        p = self.pos
        end_ns = int(self.ts[p]) + _MIN_NS if p >= 0 else self.cursor_ns
        if self.alert is not None:
            self._expire(events, end_ns)
        t = self.trade
        if t is not None and t.status == "PENDING_FILL":
            self._cancel(events, end_ns, "The stored data ended before the trade filled")
        elif t is not None:
            self._close_trade(events, p, "DATA_END", self.c[p], "close", end_ns)
        self._event(events, end_ns, "DATA_END", "No more stored bars: the session is over")
        self.over = True
        self.status = "ENDED"

    # ── persistence ───────────────────────────────────────────────────────
    def _params_json(self) -> dict[str, Any]:
        out = dict(self.setup.params)
        out["_state"] = {
            "seq": self.seq,
            "alert": self.alert,
            "alert_no": self.alert_no,
            "revealed": self.revealed,
            "next_candle_ns": self.next_candle_ns,
            "day_end": self.day_end,
            "over": self.over,
            "window": None
            if self.window is None
            else [_ts_ns(self.window.lo), _ts_ns(self.window.lo_strategy), _ts_ns(self.window.hi)],
        }
        return out

    def save(self) -> None:
        """Write the cursor and the open trade now (before the session leaves memory)."""
        self._save([], force=True)

    def _save(self, events: list[dict], force: bool = False) -> None:
        """Events are written as they happen; the cursor and the open trade with them, on a
        pause-like action (`force`) and at least every `SAVE_EVERY` revealed minutes (F8: not
        on every advance). A restart resumes from the last cursor written."""
        if not (force or events or self.revealed - self.saved_revealed >= SAVE_EVERY):
            return
        cursor = pd.Timestamp(self.cursor_ns, tz="UTC").to_pydatetime()
        t = self.trade
        trade: tuple[int, dict[str, Any]] | None = None
        if t is not None:
            row = self._trade_row(t)
            fields = {k: row[k] for k in _OPEN_KEYS}
            fields["entry_ts"] = (
                pd.Timestamp(t.entry_ns, tz="UTC").to_pydatetime() if t.entry_ns else None
            )
            fields["detail"] = t.detail()
            fields["updated_at"] = now_ist()
            trade = (t.id, fields)
        # events, trade and cursor in one transaction (server review F8)
        self.store.save_step(
            self.setup.id, events, trade, (cursor, self.status, self._params_json())
        )
        self.saved_revealed = self.revealed

    def _restore(self, row: dict[str, Any]) -> None:
        st = dict((row.get("params") or {}).get("_state") or {})
        cursor_ns = _ns_of(row["cursor_ts"])
        self.cursor_ns = cursor_ns
        win = st.get("window")
        window = None
        if self.setup.mode == "past" and win and len(win) == 3:
            # the very window the session had: the same frame, so the same signals
            window = Window(
                *(
                    None if v is None else pd.Timestamp(int(v), tz="UTC").tz_convert(IST)
                    for v in win
                )
            )
        self._load_at(cursor_ns + _MIN_NS, cursor_ns, window)
        self.pos = int(np.searchsorted(self.ts, cursor_ns, side="right")) - 1
        self.seq = max(int(st.get("seq") or 0), int(self.store.last_seq(self.setup.id)))
        self.alert = st.get("alert")
        self.alert_no = int(st.get("alert_no") or 0)
        self.revealed = int(st.get("revealed") or 0)
        self.next_candle_ns = int(st.get("next_candle_ns") or self._first_open_candle(cursor_ns))
        self.day_end = bool(st.get("day_end"))
        self.over = bool(st.get("over"))
        self.status = str(row.get("status") or "RUNNING")
        if self.status == "BUILDING":
            self.status = "RUNNING"  # stored before its first build finished: not shown yet
        self.saved_revealed = self.revealed
        for e in self.store.events(self.setup.id, MAX_EVENTS):
            self.log.append(e)
        for r in self.store.trades(session_id=self.setup.id):
            if r["status"] == "CLOSED":
                self.closed.append(closed_row(r))
            elif r["status"] == "CANCELLED":
                continue
            elif self.trade is None:
                self.trade = _trade_from_row(r)
                t = self.trade
                if t.status == "OPEN" and 0 <= self.pos < len(self.c):
                    self._rebuild_marks()
                    t.mark = float(self.c[self.pos])
                    t.mark_price = self._mark_price(t, self.pos)


_TRADE_KEYS = (
    "id",
    "status",
    "side",
    "action",
    "qty",
    "entry_ts",
    "entry",
    "entry_price",
    "source",
    "stop",
    "target",
    "stop0",
    "target0",
    "stage",
    "best",
    "progress",
    "mark",
    "mark_price",
    "open_pnl",
    "exit_ts",
    "exit",
    "exit_price",
    "exit_reason",
    "gross",
    "charges",
    "net",
    "r_multiple",
)
_OPEN_KEYS = tuple(
    k
    for k in _TRADE_KEYS
    if k not in ("id", "side", "action", "qty", "entry_ts", "exit_ts", "exit_reason")
)


def summary_of(closed: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    """The contract's `summary` of a session's closed trades."""
    nets = [float(r["net"] or 0.0) for r in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    net = sum(nets)
    return {
        "trades": len(nets),
        "winners": len(wins),
        "net": _num(net),
        "gross": _num(sum(float(r["gross"] or 0.0) for r in closed)),
        "charges": _num(sum(float(r["charges"] or 0.0) for r in closed)),
        "avg_win": _num(sum(wins) / len(wins)) if wins else None,
        "avg_loss": _num(sum(losses) / len(losses)) if losses else None,
        "best": _num(max(nets)) if nets else None,
        "worst": _num(min(nets)) if nets else None,
        "equity": _num(capital + net),
    }


def closed_row(r: dict[str, Any]) -> dict[str, Any]:
    """A stored trade row in the contract's trade shape."""
    out: dict[str, Any] = {}
    for k in _TRADE_KEYS:
        v = r.get(k)
        if isinstance(v, dt.datetime):
            v = pd.Timestamp(v).tz_convert(IST).isoformat()
        elif isinstance(v, float):
            v = _num(v, 4 if k in ("r_multiple", "progress") else 2)
        out[k] = v
    detail = r.get("detail") or {}
    out["traded"] = {"what": r.get("what"), "source": r.get("source")}
    if detail.get("note"):
        out["traded"]["note"] = detail["note"]
    out.update(_closed_extra(detail))
    return out


def _closed_extra(detail: dict[str, Any]) -> dict[str, Any]:
    """A closed trade's `lot`, `unit`, the traded stop and target when it closed, its first
    risk in rupees and its minutes (None for a row stored before US-095)."""
    c = detail.get("closing") or {}
    return {
        "lot": c.get("lot"),
        "unit": c.get("unit"),
        "stop_price": c.get("stop_price"),
        "target_price": c.get("target_price"),
        "risk_inr": _num(detail.get("risk_inr")),
        "open_r": None,
        "minutes_in_trade": c.get("minutes_in_trade"),
    }


def _trade_from_row(r: dict[str, Any]) -> TradeState:
    d = r.get("detail") or {}
    t = TradeState(
        int(r["id"]),
        1 if r["side"] == "LONG" else -1,
        int(r["qty"]),
        str(r["action"]),
        str(r["what"]),
        str(r["status"]),
        int(d.get("taken_ns") or 0),
        dict(d.get("alert") or {}),
        Contract.from_dict(d["contract"]) if d.get("contract") else None,
    )
    if d.get("plan") and d.get("state"):
        pl, s = d["plan"], d["state"]
        t.plan = ratchet.Plan(
            int(pl["side"]), float(pl["entry"]), float(pl["stop"]), float(pl["target"]),
            float(pl["cost"]),
        )  # fmt: skip
        t.state = ratchet.State(
            float(s["stop"]), float(s["target"]), float(s["best"]), int(s["stage"])
        )
    if r.get("entry_ts") is not None:
        t.entry_ns = _ns_of(r["entry_ts"])
    t.entry_price = r.get("entry_price")
    t.entry_source = r.get("source")
    t.risk_inr = d.get("risk_inr")
    t.pending_exit = d.get("pending_exit")
    t.iv_source = str(d.get("iv_source") or "vix")
    t.iv_used = d.get("iv_used")
    t.iv = float(t.iv_used) if t.iv_source == "implied" and t.iv_used else None
    return t


# ── the strategy frame, the Lab's way ───────────────────────────────────────
def build_signals(
    strategy: Strategy,
    spec: InstrumentSpec,
    series_key: str,
    timeframe: str,
    minute: pd.DataFrame,
    bars: BarSource,
    series: SeriesSpec | None = None,
) -> tuple[pd.DataFrame | None, pd.DataFrame]:
    """(the strategy's frame, the candles) on the one-minute bars `minute`, as
    `lab/runner.py: generate_for_instrument` builds it. The frame is None with too few bars."""
    candles = minute if timeframe == "1m" else resample_bars(minute, timeframe)
    if len(candles) < 60:
        return None, candles
    has_volume = bool(series.has_volume) if series is not None else True
    daily = _daily_for(bars.daily_bars(series_key), minute)
    mkt_key = market_key()
    market = None
    market_daily = None
    if mkt_key != series_key:
        mm = bars.minute_bars(mkt_key, minute.index[0], minute.index[-1] + pd.Timedelta(minutes=1))
        if not mm.empty:
            market = mm if timeframe == "1m" else resample_bars(mm, timeframe)
            market_daily = _daily_for(bars.daily_bars(mkt_key), mm)
    else:
        market_daily = daily
    ctx = Context(
        symbol=series_key,
        daily=daily,
        market=market if market is not None and not market.empty else None,
        extras={"market_daily": market_daily, "has_volume": has_volume},
    )
    if strategy.multi_timeframe:
        ctx.extras["minute_bars"] = minute
    f = strategy.generate(candles, ctx)
    return f, candles


def _daily_for(daily: pd.DataFrame | None, minute: pd.DataFrame) -> pd.DataFrame:
    """Daily bars for a frame on `minute`: the source's days before the first minute, then
    days made from the minutes themselves (`consistent_daily`), so a frame cut at the cursor
    never sees a later day's daily bar."""
    return consistent_daily(daily if daily is not None else pd.DataFrame(), minute)
