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

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST, now_ist
from ..config import BacktestCfg
from ..costs import CostModel
from ..data.expired_options import OptionBarStore, Underlying
from ..data.hygiene import consistent_daily
from ..data.resample import minutes_of, resample_bars
from ..data.universe import InstrumentSpec, SeriesSpec, market_index_key
from ..indicators import core as ind
from ..lab.runner import _session_params
from ..manage import ratchet
from ..strategies.base import Context, Strategy
from ..strategies.registry import get_strategy
from .bars import BarSource
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
HORIZON_DAYS = 30  # a past session loads this many calendar days ahead of its cursor at a time
ALERT_MINUTES = 10  # an alert lasts this many market minutes
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
    entry_wait: int = 0
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
            "entry_wait_min": self.entry_wait,
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


class SimSession:
    """A session's whole state. Not thread-safe: the manager serialises calls."""

    def __init__(
        self, setup: Setup, deps: Deps, bars: BarSource, store: Any, restore: dict | None = None
    ) -> None:
        self.setup, self.deps, self.bars, self.store = setup, deps, bars, store
        sp = _session_params(setup.spec, {})
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
        self.revealed = 0  # minutes revealed since the start
        self.status = "RUNNING"
        self.day_end = False
        self.over = False
        self.next_candle_ns = 0  # the start of the first candle not yet announced
        self.hist_start = setup.start - pd.Timedelta(days=HISTORY_DAYS)
        self.loaded_until: pd.Timestamp | None = None
        self._frame_mark: tuple[int, int] = (0, 0)
        self._frame_bucket: tuple[int, int] = (-1, -1)
        if restore is None:
            start_ns = _ns_of(setup.start)
            self._load(setup.start + pd.Timedelta(days=HORIZON_DAYS), start_ns)
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
    def _load(self, until: pd.Timestamp | None, after_ns: int | None = None) -> None:
        """Load the bars from the history start to `until`. When that leaves nothing after
        `after_ns` (a gap in the store longer than the horizon), read on to the end of the
        stored data rather than end the session while later bars exist."""
        s = self.setup
        end = None if s.mode == "live" else until
        minute = self.bars.minute_bars(s.series_key, self.hist_start, end)
        if (
            end is not None
            and after_ns is not None
            and (minute.empty or _ns_of(minute.index[-1]) <= after_ns)
        ):
            minute = self.bars.minute_bars(s.series_key, self.hist_start, None)
        self.loaded_until = until
        self._set_minutes(minute)
        self._build_frame()

    def _set_minutes(self, minute: pd.DataFrame) -> None:
        self.m = minute
        idx = pd.DatetimeIndex(minute.index)
        self.ts = idx.as_unit("ns").asi8.astype(np.int64) if len(idx) else np.zeros(0, np.int64)
        self.o = minute["open"].to_numpy(float) if len(idx) else np.zeros(0)
        self.h = minute["high"].to_numpy(float) if len(idx) else np.zeros(0)
        self.lo = minute["low"].to_numpy(float) if len(idx) else np.zeros(0)
        self.c = minute["close"].to_numpy(float) if len(idx) else np.zeros(0)
        self.v = minute["volume"].to_numpy(float) if len(idx) else np.zeros(0)
        self.day, self.mod = _local(self.ts)

    def _end_ns(self, starts: np.ndarray, minutes: int) -> np.ndarray:
        """When candles starting at `starts` close: start + length, cut at the day's close."""
        day, _ = _local(starts)
        close = day * _DAY_NS - _IST_NS + self.close_min * _MIN_NS
        return np.minimum(starts + minutes * _MIN_NS, close)

    def _build_frame(self) -> None:
        """The strategy frame, the candle closes and the plan's ATR on the loaded bars."""
        s = self.setup
        minute = self.m
        self._frame_mark = (len(self.ts), int(self.ts[-1]) if len(self.ts) else 0)
        if minute.empty:
            self.f = None
            self.cstart = self.cend = np.zeros(0, np.int64)
            self.candles = minute
            self.atr_end = np.zeros(0, np.int64)
            self.atr = np.zeros(0)
            self.pricer = self._pricer(None)
            return
        self.f, self.candles = build_signals(
            self.strategy, s.spec, s.series_key, s.timeframe, minute, self.bars, s.series
        )
        self.cstart = pd.DatetimeIndex(self.candles.index).as_unit("ns").asi8.astype(np.int64)
        self.cend = self._end_ns(self.cstart, s.tf_minutes)
        f = self.f
        if f is not None:
            self.le = f["long_entry"].to_numpy(bool)
            self.se = f["short_entry"].to_numpy(bool)
            self.lx = f["long_exit"].to_numpy(bool)
            self.sx = f["short_exit"].to_numpy(bool)
            self.lsc = np.nan_to_num(f["long_score"].to_numpy(float))
            self.ssc = np.nan_to_num(f["short_score"].to_numpy(float))
            self.fstart = pd.DatetimeIndex(f.index).as_unit("ns").asi8.astype(np.int64)
        atr_tf = atr_timeframe(s.timeframe, str(s.params.get("stop_atr_tf", "60m")))
        ab = resample_bars(minute, atr_tf)
        self.atr = ind.atr(ab["high"], ab["low"], ab["close"], ATR_PERIOD).to_numpy(float)
        astart = pd.DatetimeIndex(ab.index).as_unit("ns").asi8.astype(np.int64)
        self.atr_end = self._end_ns(astart, minutes_of(atr_tf))
        self.pricer = self._pricer(minute)

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

    def _more_past_data(self) -> bool:
        """Extend a past session's bars when the cursor reached the last one loaded."""
        if self.setup.mode != "past" or self.loaded_until is None:
            return False
        rng = self.bars.minute_range(self.setup.series_key)
        if rng is None or _ns_of(rng[1]) <= int(self.ts[-1]):
            return False
        cursor = self.cursor_ns
        self._load(
            pd.Timestamp(cursor, tz="UTC").tz_convert(IST) + pd.Timedelta(days=HORIZON_DAYS), cursor
        )
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
        if self.f is None or bucket != self._frame_bucket:
            self._set_minutes(minute)
            self._build_frame()
            self._frame_bucket = bucket
        else:
            self._set_minutes(minute)
            self._frame_mark = (len(self.ts), last)
        self.pos = int(np.searchsorted(self.ts, self.cursor_ns, side="right")) - 1

    # ── views ─────────────────────────────────────────────────────────────
    @property
    def cursor(self) -> str:
        return str(_iso(self.cursor_ns))

    def candles_view(self) -> list[list[float | int | None]]:
        """Up to 300 closed candles that end at or before the cursor, oldest first."""
        n = int(np.searchsorted(self.cend, self.cursor_ns + _MIN_NS, side="right"))
        lo = max(0, n - MAX_CANDLES)
        cd = self.candles.iloc[lo:n]
        return [
            [int(t // _NS), _num(o), _num(h), _num(low), _num(c), _num(v, 0)]
            for t, o, h, low, c, v in zip(
                self.cstart[lo:n],
                cd["open"].to_numpy(float),
                cd["high"].to_numpy(float),
                cd["low"].to_numpy(float),
                cd["close"].to_numpy(float),
                cd["volume"].to_numpy(float),
                strict=True,
            )
        ]

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
        if self.kind == "STOCK":
            return "share prices from the one-minute bars, with the Lab's slippage and charges"
        if self.setup.mode == "live":
            note = "today's traded premiums through the desk where it has them, otherwise modelled"
        elif self.deps.option_store is None:
            note = "modelled premiums (Black-76): no expired-options store was found"
        else:
            note = "real traded premiums where stored, otherwise modelled"
        return note + "." + self.vix_note() if self.vix_note() else note

    def session_view(self) -> dict[str, Any]:
        s = self.setup
        return {
            "id": s.id,
            "instrument": {"key": s.instrument_key, "label": s.label, "kind": s.kind},
            "strategy": s.strategy,
            "timeframe": s.timeframe,
            "tf_minutes": s.tf_minutes,
            "mode": s.mode,
            "start": s.start.isoformat(),
            "cursor": self.cursor,
            "status": self.status,
            "capital": _num(s.params["capital"]),
            "params": {
                "stop_atr_k": float(s.params["stop_atr_k"]),
                "target_r": float(s.params["target_r"]),
                "extend": bool(s.params["extend"]),
                "auto_exit": bool(s.params["auto_exit"]),
                "stop_atr_tf": str(s.params.get("stop_atr_tf", "60m")),
            },
            "premium_note": self.premium_note(),
        }

    def alert_view(self) -> dict[str, Any] | None:
        if self.alert is None:
            return None
        return {k: v for k, v in self.alert.items() if not k.startswith("_")}

    def trade_view(self, t: TradeState | None = None) -> dict[str, Any] | None:
        t = t or self.trade
        if t is None:
            return None
        return self._trade_row(t)

    def summary(self) -> dict[str, Any]:
        nets = [float(r["net"] or 0.0) for r in self.closed]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        net = sum(nets)
        return {
            "trades": len(nets),
            "winners": len(wins),
            "net": _num(net),
            "gross": _num(sum(float(r["gross"] or 0.0) for r in self.closed)),
            "charges": _num(sum(float(r["charges"] or 0.0) for r in self.closed)),
            "avg_win": _num(sum(wins) / len(wins)) if wins else None,
            "avg_loss": _num(sum(losses) / len(losses)) if losses else None,
            "best": _num(max(nets)) if nets else None,
            "worst": _num(min(nets)) if nets else None,
            "equity": _num(float(self.setup.params["capital"]) + net),
        }

    def state(self) -> dict[str, Any]:
        return {
            "session": self.session_view(),
            "alert": self.alert_view(),
            "trade": self.trade_view(),
            "summary": self.summary(),
            "trades": list(self.closed),
        }

    def last_price(self) -> float | None:
        return _num(self.c[self.pos]) if 0 <= self.pos < len(self.c) else None

    # ── events ────────────────────────────────────────────────────────────
    def _event(
        self, out: list[dict], ns: int, kind: str, text: str, data: dict | None = None
    ) -> None:
        self.seq += 1
        out.append(
            {
                "seq": self.seq,
                "ts": _iso(ns),
                "kind": kind,
                "text": text,
                "data": data or {},
                "_ts": pd.Timestamp(ns, tz="UTC").to_pydatetime(),
            }
        )

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
        self._save([])
        return self.state()

    def skip(self, alert_id: str) -> dict[str, Any]:
        if self.alert is None or self.alert["id"] != alert_id:
            raise SimError(409, "There is no such alert to skip: it expired or was taken.")
        self.alert = None
        self._save([])
        return self.state()

    def close(self) -> dict[str, Any]:
        t = self.trade
        if t is None:
            raise SimError(409, "There is no open trade to close.")
        if t.status == "PENDING_FILL":
            self.store.drop_trade(t.id)
            self.trade = None
        else:
            t.pending_exit = "MANUAL"
        self._save([])
        return self.state()

    def end(self) -> dict[str, Any]:
        """Close an open trade at the next minute's open (the last close when there is no
        next minute) and mark the session ENDED."""
        shown: list[dict] = []
        events: list[dict] = []
        t = self.trade
        if t is not None and t.status == "PENDING_FILL":
            self.store.drop_trade(t.id)
            self.trade = None
        elif t is not None and self.status != "ENDED":
            t.pending_exit = "MANUAL"
            if self.setup.mode == "past" and self.pos + 1 < len(self.ts):
                shown = list(self.advance(1)["events"])  # saved by advance
            if self.trade is not None and self.pos >= 0:  # no next minute: the last close
                p = self.pos
                end_ns = int(self.ts[p]) + _MIN_NS
                self._close_trade(events, p, "MANUAL", self.c[p], "close", end_ns)
        self.alert = None
        self.status = "ENDED"
        self._save(events)
        del shown  # its events are saved; the answer is the state, as GET gives it
        return self.state()

    # ── the clock: reveal minutes ─────────────────────────────────────────
    def advance(self, minutes: int = 1) -> dict[str, Any]:
        if self.status == "ENDED":
            raise SimError(409, "The session has ended.")
        events: list[dict] = []
        revealed: list[list] = []
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
            self._minute(p, events)
            if self._is_day_end(p):
                self._day_end(p, events)
                break
        if self.setup.mode == "past" and not self.over and self.pos + 1 >= len(self.ts):
            if not self._more_past_data():
                self._data_end(events)
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
            stop_price = pr.option_exit(pr.model_premium(contract, stop, when))
            target_price = pr.option_exit(pr.model_premium(contract, target, when))
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
            },
            "risk_inr": _num(risk_inr),
            "reward_inr": _num(reward_inr),
            "costs_inr": _num(costs),
            "reasons": reasons,
            "_rev": self.revealed,
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
            self.store.drop_trade(t.id)
            self.trade = None
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
            t.entry_wait = q.wait
            delta = max(pr.delta(t.contract, o, minute), 0.05)
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
            stop_fill = pr.option_exit(pr.model_premium(t.contract, plan.stop, minute))
            t.risk_inr = max(entry_price - stop_fill, pr.tick) * pr.qty
        else:
            t.risk_inr = plan.risk * pr.qty
        t.mark, t.mark_price = entry, entry_price
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
        q = pr.premium(t.contract, pd.Timestamp(int(self.ts[p]), tz="UTC"), bar, bar[3], "close")
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
            q = pr.premium(t.contract, minute, bar, level, how)
            exit_price = pr.option_exit(q.price)
            exit_level = level
            exit_source = q.source
            exit_wait = q.wait
        else:
            exit_wait = 0
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
        detail["exit_wait_min"] = exit_wait
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
        }

    # ── the day and the data ──────────────────────────────────────────────
    def _day_end(self, p: int, events: list[dict]) -> None:
        end_ns = int(self.ts[p]) + _MIN_NS
        self._close_candles(end_ns + self.setup.tf_minutes * _MIN_NS, p, events)
        if self.alert is not None:
            self._expire(events, end_ns)
        t = self.trade
        if t is not None and t.status == "PENDING_FILL":
            self.store.drop_trade(t.id)
            self.trade = None
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
            self.store.drop_trade(t.id)
            self.trade = None
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
        }
        return out

    def _save(self, events: list[dict]) -> None:
        cursor = pd.Timestamp(self.cursor_ns, tz="UTC").to_pydatetime()
        self.store.add_events(self.setup.id, events)
        t = self.trade
        if t is not None:
            row = self._trade_row(t)
            fields = {k: row[k] for k in _OPEN_KEYS}
            fields["entry_ts"] = (
                pd.Timestamp(t.entry_ns, tz="UTC").to_pydatetime() if t.entry_ns else None
            )
            fields["detail"] = t.detail()
            fields["updated_at"] = now_ist()
            self.store.save_trade(t.id, fields)
        self.store.save_session(self.setup.id, cursor, self.status, self._params_json())

    def _restore(self, row: dict[str, Any]) -> None:
        st = dict((row.get("params") or {}).get("_state") or {})
        cursor_ns = _ns_of(row["cursor_ts"])
        self.cursor_ns = cursor_ns
        if self.setup.mode == "past":
            self._load(
                pd.Timestamp(cursor_ns, tz="UTC").tz_convert(IST) + pd.Timedelta(days=HORIZON_DAYS),
                cursor_ns,
            )
        else:
            self._load(None)
        self.pos = int(np.searchsorted(self.ts, cursor_ns, side="right")) - 1
        self.seq = max(int(st.get("seq") or 0), int(self.store.last_seq(self.setup.id)))
        self.alert = st.get("alert")
        self.alert_no = int(st.get("alert_no") or 0)
        self.revealed = int(st.get("revealed") or 0)
        self.next_candle_ns = int(st.get("next_candle_ns") or self._first_open_candle(cursor_ns))
        self.day_end = bool(st.get("day_end"))
        self.over = bool(st.get("over"))
        self.status = str(row.get("status") or "RUNNING")
        for r in self.store.trades(session_id=self.setup.id):
            if r["status"] == "CLOSED":
                self.closed.append(closed_row(r))
            elif self.trade is None:
                self.trade = _trade_from_row(r)
                t = self.trade
                if t.status == "OPEN" and 0 <= self.pos < len(self.c):
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
    return out


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
    t.entry_wait = int(d.get("entry_wait_min") or 0)
    return t


def atr_timeframe(timeframe: str, stop_atr_tf: str) -> str:
    """The bars the first stop's ATR is read on: 60m when `stop_atr_tf` is "60m" and the
    strategy's timeframe is shorter than 60m, otherwise the strategy's own timeframe."""
    if stop_atr_tf == "60m" and minutes_of(timeframe) < 60:
        return "60m"
    return timeframe


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
    mkt_key = market_index_key()
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
