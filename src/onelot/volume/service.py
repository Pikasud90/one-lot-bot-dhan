"""The Volume Watch inside the desk (US-089, `.agile/stories/VOL-API.md`).

* `Baselines`: for one session date, built from the bars BEFORE it (the store, joined with
  the bars the running desk already holds): each stock's expected turnover and spike-score
  mean and sd at every minute, the index influence weights, the previous close. Built once a
  session off the desk's poll (a thread), rebuilt when the session date changes.
* `DayView`: one session's minutes for the basket, from the day's closed one-minute bars and
  the baselines; the body of `/api/volume` and the alert decisions are read from it.
* `VolumeWatch`: the step in the desk's poll (about once a minute while the market is open:
  alerts and the order book), and the four routes' bodies.

A watch alert is not a trade alert: it never opens a paper trade and never says buy or sell.
Nothing here asks the broker for bars; the order book is one quote request a minute.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from ..clock import IST, now_ist
from ..data.store import _read_bars
from ..data.universe import load_specs
from ..desk.config import DeskConfig, VolumeCfg, load_desk_config
from ..notify.base import Notification, NotificationLevel
from ..store.schema import METADATA, volume_alerts, volume_book
from . import indicator as ind

log = logging.getLogger(__name__)

EVIDENCE_PATH = Path(__file__).with_name("evidence.json")
HISTORY_DAYS = 100  # calendar days read before a session: about 65 sessions
BASKET_FULL = 0.8  # a session is full for the basket when this part of its stocks are full
PRESSURE_WORDS = 0.1  # |pressure_5m| below this reads "no clear pressure"
BOOK_KEEP_DAYS = 60
BOOK_FRESH = dt.timedelta(minutes=3)
BOOK_MAX_FAILURES = 3
AUCTION_FROM = dt.date(2026, 8, 3)  # stock bars end at 15:14 from this session
CACHE_DAYS = 8
DEFAULT_INDEX = "NIFTY"
#: the indices `?index=` may choose for the price line (VOL-API.md `index_options`)
INDEX_OPTIONS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY")


class VolumeError(Exception):
    """A request the watch cannot answer: `status` and a plain sentence."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _r(x: Any, nd: int = 2) -> float | None:
    v = ind.finite(x)
    return round(v, nd) if v is not None else None


def _days(df: pd.DataFrame) -> np.ndarray:
    """The IST session date of each row as numpy days (fast: no Python date objects)."""
    return np.asarray(ind.session_dates(df.index).to_numpy().astype("datetime64[D]"))


def _day_of(df: pd.DataFrame | None, day: dt.date) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    out = df.loc[_days(df) == np.datetime64(day, "D")]
    return out if not out.empty else None


def _before(df: pd.DataFrame | None, day: dt.date) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    out = df.loc[_days(df) < np.datetime64(day, "D")]
    return out if not out.empty else None


def _join(a: pd.DataFrame | None, b: pd.DataFrame | None) -> pd.DataFrame | None:
    parts = [f for f in (a, b) if f is not None and not f.empty]
    if not parts:
        return None
    both = pd.concat([p[["open", "high", "low", "close", "volume"]] for p in parts])
    both.index = pd.DatetimeIndex(both.index).tz_convert(IST)
    return both[~both.index.duplicated(keep="last")].sort_index()


def _names(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


# ── reading bars ─────────────────────────────────────────────────────────
class StoreReader:
    """Read queries on the app's store (any engine, a read-only one included)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def minutes(self, key: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame | None:
        df = _read_bars(self.engine, key, "1m", start, end)
        if df is None or df.empty:
            return None
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    def span(self, key: str) -> tuple[dt.date, dt.date] | None:
        from ..store.schema import bars

        q = select(func.min(bars.c.ts), func.max(bars.c.ts)).where(
            bars.c.series_key == key, bars.c.resolution == "1m"
        )
        with self.engine.connect() as conn:
            lo, hi = conn.execute(q).one()
        if lo is None or hi is None:
            return None
        return (_utc_to_ist(lo).date(), _utc_to_ist(hi).date())


def _utc_to_ist(v: Any) -> dt.datetime:
    t = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(IST)


# ── baselines ────────────────────────────────────────────────────────────
@dataclass
class Baselines:
    day: dt.date
    hist: dict[str, pd.DataFrame]  # turnover day matrices, sessions before `day`
    full: dict[str, pd.Series]
    expected: dict[str, dict[str, np.ndarray]]  # window -> the row of `day` (1m, 5m, day)
    mu: dict[str, np.ndarray]
    sd: dict[str, np.ndarray]
    prev_close: dict[str, float]
    tail: dict[str, pd.DataFrame]  # the last session before `day` (the bvc rule's warm-up)
    weights: dict[str, tuple[dict[str, float], float | None]]
    built_in: float = 0.0
    _basket: dict[frozenset[str], dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def expected_hist(self, keys: list[str], idx: pd.DatetimeIndex) -> pd.DataFrame:
        """The basket's expected turnover for each history session (the profile's day rvol)."""
        eb = np.zeros((len(idx), ind.SESSION_MINUTES))
        for s in keys:
            h = self.hist[s].reindex(idx)
            eb += ind.expected_matrix(h, self.full[s].reindex(idx).fillna(False)).to_numpy()
        return pd.DataFrame(eb, index=idx, columns=range(ind.SESSION_MINUTES))

    def has_history(self, key: str) -> bool:
        e = self.expected.get(key)
        return e is not None and float(np.nansum(e["1m"])) > 0

    def basket(self, keys: list[str]) -> dict[str, Any]:
        """For a set of stocks: their summed turnover matrix over the history, its full
        sessions, the expected turnover matrix, and the spike score's mean and sd for `day`."""
        k = frozenset(keys)
        with self._lock:
            hit = self._basket.get(k)
            if hit is not None:
                return hit
        rows = sorted(set().union(*(set(self.hist[s].index) for s in keys))) if keys else []
        idx = pd.DatetimeIndex(rows)
        tb = np.zeros((len(idx), ind.SESSION_MINUTES))
        nfull = np.zeros(len(idx))
        for s in keys:
            h = self.hist[s].reindex(idx)
            tb += np.nan_to_num(h.to_numpy())
            nfull += self.full[s].reindex(idx).fillna(False).to_numpy(dtype=bool)
        need = math.ceil(BASKET_FULL * len(keys)) if keys else 1
        full = pd.Series(nfull >= max(need, 1), index=idx)
        mu, sd = ind.zstats_row(tb, full.to_numpy())
        out = {
            "exp": ind.expected_windows_row(tb, full.to_numpy()),
            "keys": list(keys),
            "tb": pd.DataFrame(tb, index=idx, columns=range(ind.SESSION_MINUTES)),
            "full": full,
            "mu": mu,
            "sd": sd,
        }
        with self._lock:
            self._basket[k] = out
        return out


def build_baselines(
    day: dt.date,
    reader: StoreReader | None,
    basket: list[str],
    indices: list[str],
    extra: Callable[[str], pd.DataFrame | None] = lambda _k: None,
) -> Baselines:
    """The baselines of `day` from the bars before it: the store's (read queries only) and
    `extra(key)` (the desk's own bars), the later winning a timestamp both have."""
    t0 = time.perf_counter()
    start = dt.datetime.combine(day - dt.timedelta(days=HISTORY_DAYS), dt.time(0), IST)
    end = dt.datetime.combine(day, dt.time(0), IST)

    def history(key: str) -> pd.DataFrame | None:
        stored = reader.minutes(key, start, end) if reader is not None else None
        return _before(_join(stored, _before(extra(key), day)), day)

    hist: dict[str, pd.DataFrame] = {}
    full: dict[str, pd.Series] = {}
    expected: dict[str, dict[str, np.ndarray]] = {}
    mu: dict[str, np.ndarray] = {}
    sd: dict[str, np.ndarray] = {}
    prev_close: dict[str, float] = {}
    tail: dict[str, pd.DataFrame] = {}
    closes: dict[str, pd.DataFrame] = {}
    for s in basket:
        bars = history(s)
        if bars is None:
            continue
        closes[s] = bars
        m = ind.day_matrix(ind.turnover(bars))
        f = ind.full_days(m)
        filled = m.copy()
        filled.loc[f.to_numpy()] = filled.loc[f.to_numpy()].fillna(0.0)
        hist[s], full[s] = filled, f
        expected[s] = ind.expected_windows_row(m.to_numpy(), f.to_numpy())
        mu[s], sd[s] = ind.zstats_row(m.to_numpy(), f.to_numpy())
        prev_close[s] = float(bars["close"].iloc[-1])
        d = _days(bars)
        tail[s] = bars.loc[d == d[-1]]
    base = Baselines(day, hist, full, expected, mu, sd, prev_close, tail, {})
    # influence: the previous 60 full sessions of the basket
    keys = [s for s in basket if s in hist]
    if keys:
        b = base.basket(keys)
        fdays = (
            pd.DatetimeIndex(b["full"].index[b["full"].to_numpy()])[-ind.Z_LOOKBACK :]
            .to_numpy()
            .astype("datetime64[D]")
        )
        rets = {}
        for s in keys:
            bars = closes[s]
            sel = np.isin(_days(bars), fdays)
            rets[s] = ind.minute_returns(bars.loc[sel, "close"])
        x = pd.DataFrame(rets)
        for k in indices:
            ib = history(k)
            if ib is None:
                continue
            base.prev_close[k] = float(ib["close"].iloc[-1])
            sel = np.isin(_days(ib), fdays)
            y = ind.minute_returns(ib.loc[sel, "close"])
            try:
                w, r2 = ind.influence(y, x)
            except Exception:  # noqa: BLE001 - a failed fit leaves the index without weights
                log.exception("volume: influence of %s failed", k)
                continue
            base.weights[k] = ({s: float(v) for s, v in w.items()}, r2)
    base.built_in = time.perf_counter() - t0
    return base


# ── one session's view ───────────────────────────────────────────────────
@dataclass
class DayView:
    day: dt.date
    last_col: int
    keys: list[str]  # stocks in the totals (bars today and a history)
    missing: list[str]  # basket stocks without bars that day
    no_history: list[str]
    t: dict[str, np.ndarray]  # per stock turnover row (0 to last_col)
    buy: dict[str, np.ndarray]
    exp: dict[str, dict[str, np.ndarray]]  # stock -> window -> expected row
    z: dict[str, np.ndarray]
    last: dict[str, float]
    tb: np.ndarray
    eb: dict[str, np.ndarray]  # the basket's expected rows by window (a median of sums)
    buy_b: np.ndarray
    zb: np.ndarray
    rv: dict[str, np.ndarray]

    @property
    def sell_b(self) -> np.ndarray:
        return self.tb - self.buy_b

    def stock_rv(self, s: str) -> dict[str, np.ndarray]:
        e = self.exp[s]
        return ind.rvol_rows(self.t[s], e["1m"], e["5m"], e["day"])

    def win(self, a: np.ndarray, col: int) -> float:
        return float(ind.window_sum(a)[0, col])

    def ts(self, col: int) -> dt.datetime:
        m = ind.OPEN_MINUTE + col
        return dt.datetime.combine(self.day, dt.time(m // 60, m % 60), IST)


def compute_view(
    day: dt.date, bars: dict[str, pd.DataFrame], base: Baselines, basket: list[str], rule: str
) -> DayView | None:
    """The session's view from its closed one-minute bars; None when no basket stock has one.
    The newest minute is the latest one at or after which at least half the stocks with bars
    have a bar (a stock whose last minute has not arrived yet does not pull the basket down)."""
    present = [s for s in basket if bars.get(s) is not None and not bars[s].empty]
    if not present:
        return None
    missing = [s for s in basket if s not in present]
    rows_t: dict[str, np.ndarray] = {}
    rows_b: dict[str, np.ndarray] = {}
    lasts: dict[str, int] = {}
    last_px: dict[str, float] = {}
    today = np.datetime64(day, "D")
    for s in present:
        b = bars[s]
        warm = base.tail.get(s)
        both = _join(warm, b) if warm is not None else b
        assert both is not None
        a = both[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
        u = ind.up_share_values(a[:, 0], a[:, 1], a[:, 2], a[:, 3],
                                ind.first_of_session(both.index), rule)  # fmt: skip
        on = _days(both) == today
        col = ind.minute_of_day(both.index)[on] - ind.OPEN_MINUTE
        keep = (col >= 0) & (col < ind.SESSION_MINUTES)
        if not keep.any():
            continue
        col = col[keep]
        turn = (a[on, 4] * a[on, 3])[keep]
        rows_t[s] = np.full(ind.SESSION_MINUTES, np.nan)
        rows_b[s] = np.full(ind.SESSION_MINUTES, np.nan)
        rows_t[s][col] = turn
        rows_b[s][col] = turn * u[on][keep]
        lasts[s] = int(col.max())
        last_px[s] = float(a[on, 3][keep][-1])
    if not lasts:
        return None
    ordered = sorted(lasts.values(), reverse=True)
    last_col = ordered[math.ceil(len(ordered) / 2) - 1]
    keys = [s for s in lasts if base.has_history(s)]
    no_hist = [s for s in lasts if s not in keys]
    t, buy, exp, z = {}, {}, {}, {}
    for s in lasts:
        t[s] = ind.fill_through(rows_t[s], last_col)
        buy[s] = ind.fill_through(rows_b[s], last_col)
        e = base.expected.get(s)
        exp[s] = {w: (np.zeros(ind.SESSION_MINUTES) if e is None else np.nan_to_num(e[w]))
                  for w in ind.WINDOWS}  # fmt: skip
        if s in base.mu:
            z[s] = ind.zscore(ind.log_window(t[s]), base.mu[s], base.sd[s])[0]
        else:
            z[s] = np.full(ind.SESSION_MINUTES, np.nan)
    tb = np.where(np.arange(ind.SESSION_MINUTES) <= last_col, 0.0, np.nan)
    bb = tb.copy()
    for s in keys:
        tb = tb + t[s]
        bb = bb + buy[s]
    bs = base.basket(keys) if keys else None
    zero = np.zeros(ind.SESSION_MINUTES)
    eb = bs["exp"] if bs is not None else dict.fromkeys(ind.WINDOWS, zero)
    zb = (
        ind.zscore(ind.log_window(tb), bs["mu"], bs["sd"])[0]
        if bs is not None
        else np.full(ind.SESSION_MINUTES, np.nan)
    )
    return DayView(
        day, last_col, keys, missing, no_hist, t, buy, exp, z, last_px, tb, eb, bb, zb,
        ind.rvol_rows(tb, eb["1m"], eb["5m"], eb["day"]),
    )  # fmt: skip


# ── the watch ────────────────────────────────────────────────────────────
class VolumeWatch:
    """The Volume Watch of one running desk (or of none: stored sessions only)."""

    def __init__(
        self,
        engine: Engine,
        desk: Any = None,
        notifier: Any = None,
        cfg: VolumeCfg | None = None,
        desk_cfg: DeskConfig | None = None,
        sync: bool = False,
        clock: Callable[[], dt.datetime] = now_ist,
        create_tables: bool = True,
    ) -> None:
        self.engine = engine
        self.desk = desk
        self.notifier = notifier
        dc = desk_cfg or (getattr(desk, "cfg", None) if desk is not None else None)
        dc = dc or load_desk_config()
        self.desk_cfg = dc
        self.cfg = cfg or dc.volume
        self.sync = sync  # tests: build baselines in the caller's thread
        self.clock = clock
        self.reader = StoreReader(engine)
        self.series, _ = load_specs()
        self.basket = list(self.cfg.basket) or [s.key for s in dc.stocks]
        self.index_cfg = [(i.key, i.trade) for i in dc.indices] or [
            (k, True) for k in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")
        ]
        self.indices = [k for k, _ in self.index_cfg]
        self.errors = 0
        self.last_error: str | None = None
        self.book_failures = 0
        self.book_error: str | None = None
        self.book_last: dict[str, Any] | None = None
        self._book_minute: dt.datetime | None = None
        self._step_minute: dt.datetime | None = None
        self._processed: dict[dt.date, int] = {}
        self._pruned: dt.date | None = None
        self._lock = threading.Lock()
        self._bases: OrderedDict[dt.date, Baselines] = OrderedDict()
        self._building: dict[dt.date, threading.Thread] = {}
        self._build_errors: dict[dt.date, str] = {}
        self._bodies: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        if create_tables:  # a read-only store (the replay tool) gets none
            METADATA.create_all(engine, tables=[volume_alerts, volume_book])

    # labels ------------------------------------------------------------
    def index_options(self) -> list[dict[str, str]]:
        return [{"key": k, "label": self.label(k)} for k in INDEX_OPTIONS]

    def label(self, key: str) -> str:
        s = self.series.get(key)
        return s.label if s is not None else key

    # bars --------------------------------------------------------------
    def _desk_bars(self, key: str) -> pd.DataFrame | None:
        desk = self.desk
        if desk is None or int(getattr(desk, "fine_minutes", 1) or 1) != 1:
            return None
        t = getattr(desk, "trackers", {}).get(key)
        if t is not None:
            return t.bars
        return getattr(desk, "context_bars", {}).get(key)

    def _live_day(self) -> dt.date | None:
        """Today when the desk holds one-minute bars of today for a basket stock."""
        today = self.clock().date()
        for s in self.basket:
            if _day_of(self._desk_bars(s), today) is not None:
                return today
        return None

    def _market_open(self) -> bool:
        desk = self.desk
        if desk is None:
            return False
        try:
            return bool(desk._market_open(self.clock()))
        except Exception:  # noqa: BLE001
            return False

    def _stored_bars(self, key: str, day: dt.date) -> pd.DataFrame | None:
        start = dt.datetime.combine(day, dt.time(0), IST)
        got = self.reader.minutes(key, start, start + dt.timedelta(days=1))
        if day == self.clock().date():
            got = _join(got, _day_of(self._desk_bars(key), day))
        return _day_of(got, day)

    def last_stored_day(self) -> dt.date | None:
        best: dt.date | None = None
        for s in self.basket[:3]:
            span = self.reader.span(s)
            if span is not None and (best is None or span[1] > best):
                best = span[1]
        return best

    def _span(self) -> tuple[dt.date, dt.date] | None:
        lo: dt.date | None = None
        hi: dt.date | None = None
        for s in self.basket[:3]:
            span = self.reader.span(s)
            if span is None:
                continue
            lo = span[0] if lo is None else min(lo, span[0])
            hi = span[1] if hi is None else max(hi, span[1])
        return (lo, hi) if lo is not None and hi is not None else None

    # baselines ----------------------------------------------------------
    def baselines(self, day: dt.date, wait: bool) -> Baselines | None:
        with self._lock:
            hit = self._bases.get(day)
            if hit is not None:
                self._bases.move_to_end(day)
                return hit
            running = self._building.get(day)
        if wait or self.sync:
            if running is not None:
                running.join(timeout=30)
                with self._lock:
                    if day in self._bases:
                        return self._bases[day]
            return self._build(day)
        if running is None:
            th = threading.Thread(target=self._build, args=(day,), name="volume-baselines")
            with self._lock:
                self._building[day] = th
            th.daemon = True
            th.start()
        return None

    def _build(self, day: dt.date) -> Baselines | None:
        try:
            base = build_baselines(day, self.reader, self.basket, self.indices, self._desk_bars)
        except Exception as exc:  # noqa: BLE001 - shown in notes, the desk carries on
            log.exception("volume: baselines for %s failed", day)
            with self._lock:
                self._build_errors[day] = type(exc).__name__
                self._building.pop(day, None)
            return None
        with self._lock:
            self._bases[day] = base
            self._bases.move_to_end(day)
            while len(self._bases) > CACHE_DAYS:
                self._bases.popitem(last=False)
            self._building.pop(day, None)
            self._build_errors.pop(day, None)
        log.info("volume: baselines for %s built in %.1fs", day, base.built_in)
        return base

    # the poll step ------------------------------------------------------
    def on_poll(self, now: dt.datetime, market_open: bool) -> None:
        """About once a minute while the market is open: decide alerts on the newly closed
        minutes and read the order book. With the market shut: warm the baselines of the
        session the page shows and prune old book readings."""
        minute = now.replace(second=0, microsecond=0)
        if minute == self._step_minute:
            return
        self._step_minute = minute
        if not market_open:
            self._prune(now)
            day = self._live_day() or self.last_stored_day()
            if day is not None:
                self.baselines(day, wait=False)
            return
        day = now.date()
        base = self.baselines(day, wait=False)
        if base is None:
            return
        if self.cfg.alerts_on:
            view = self._view(day, base, live=True)
            if view is not None:
                self._decide(view, base)
        if self.cfg.book:
            self._read_book(now)

    def _view(self, day: dt.date, base: Baselines, live: bool) -> DayView | None:
        if live:
            bars = {s: _day_of(self._desk_bars(s), day) for s in self.basket}
        else:
            bars = {s: self._stored_bars(s, day) for s in self.basket}
        return compute_view(day, {k: v for k, v in bars.items() if v is not None}, base,
                            self.basket, self.cfg.rule)  # fmt: skip

    def _candidates(self, view: DayView, start: int) -> list[ind.Candidate]:
        out = []
        for c in range(max(start, 0), view.last_col + 1):
            z = view.zb[c]
            if np.isfinite(z):
                out.append(ind.Candidate(c, "MARKET", "MARKET", float(z)))
            for s in view.keys:
                zs = view.z[s][c]
                if np.isfinite(zs):
                    out.append(ind.Candidate(c, "STOCK", s, float(zs)))
        return out

    def _pick(self, view: DayView, start: int, earlier: list[tuple[int, str, str]]) -> list:
        cfg = self.cfg
        return ind.pick_alerts(
            self._candidates(view, start),
            z_market=cfg.z_alert,
            z_stock=cfg.stock_z_alert,
            cooldown_min=cfg.cooldown_min,
            cap=cfg.max_alerts_per_day,
            earlier=earlier,
        )

    def stored_alerts(self, session: dt.date) -> list[dict[str, Any]]:
        q = (
            select(volume_alerts.c.payload, volume_alerts.c.session)
            .where(volume_alerts.c.session == session.isoformat())
            .order_by(volume_alerts.c.ts.desc(), volume_alerts.c.id)
        )
        with self.engine.connect() as conn:
            return [dict(r.payload) for r in conn.execute(q)]

    def _decide(self, view: DayView, base: Baselines) -> list[dict[str, Any]]:
        """New alerts on the minutes not decided yet (at most the last three: an alert is
        raised in time or not at all), stored once by id and sent through the Notifier."""
        stored = self.stored_alerts(view.day)
        earlier = [(_col(a["ts"]), a["scope"], a["key"]) for a in stored]
        start = max(self._processed.get(view.day, -1) + 1, view.last_col - 2)
        self._processed[view.day] = view.last_col
        new = []
        for c in self._pick(view, start, earlier):
            obj = self.alert_obj(view, base, c)
            try:
                with self.engine.begin() as conn:
                    conn.execute(
                        insert(volume_alerts).values(
                            id=obj["id"],
                            session=view.day.isoformat(),
                            ts=view.ts(c.t),
                            scope=c.scope,
                            key=c.key,
                            payload=obj,
                            sent=False,
                            created_at=self.clock(),
                        )  # fmt: skip
                    )
            except IntegrityError:
                continue  # raised before (a restart): never twice
            new.append(obj)
            if self.cfg.notify and self.notifier is not None:
                self._send(obj)
        return new

    def _send(self, obj: dict[str, Any]) -> None:
        body = obj["text"] + (" " + obj["evidence"] if obj.get("evidence") else "")
        n = Notification(
            title=obj["title"],
            body=body,
            level=NotificationLevel.WARNING,
            payload=dict(obj),
            kind="volume",
            dedup_key=f"volume:{obj['id']}",
        )
        self.notifier.send(n, dedup_key=f"volume:{obj['id']}")
        with self.engine.begin() as conn:
            conn.execute(
                update(volume_alerts).where(volume_alerts.c.id == obj["id"]).values(sent=True)
            )

    def alert_obj(self, view: DayView, base: Baselines, c: ind.Candidate) -> dict[str, Any]:
        col = c.t
        if c.scope == "MARKET":
            a5, e5 = view.win(view.tb, col), float(view.eb["5m"][col])
            fs = ind.flow_share(view.win(view.buy_b, col), a5 - view.win(view.buy_b, col))
            excess = {
                s: view.win(view.t[s], col) - float(view.exp[s]["5m"][col]) for s in view.keys
            }
            leaders = sorted(excess, key=lambda k: -excess[k])[:2]
            net5 = {s: view.win(2 * view.buy[s] - view.t[s], col) for s in view.keys}
            exp5 = {s: float(view.exp[s]["5m"][col]) for s in view.keys}
            likely = ind.likely_indices(
                {k: base.weights.get(k, ({}, None))[0] for k, trade in self.index_cfg if trade},
                net5,
                exp5,
            )
        else:
            s = c.key
            a5, e5 = view.win(view.t[s], col), float(view.exp[s]["5m"][col])
            fs = ind.flow_share(view.win(view.buy[s], col), a5 - view.win(view.buy[s], col))
            leaders = [s]
            score = {
                k: base.weights.get(k, ({}, None))[0].get(s, 0.0)
                for k, trade in self.index_cfg
                if trade
            }
            likely = [k for k in sorted(score, key=lambda k: -score[k]) if score[k] >= 0.05][:2]
        rvol = ind.safe_ratio(a5, e5)
        way = ind.direction(fs)
        words = {"BUYING": "mostly buying", "SELLING": "mostly selling", "TWO_WAY": "two-way"}
        times = f"{rvol:.1f} times normal" if np.isfinite(rvol) else "far above normal"
        who = "" if c.scope == "MARKET" else self.label(c.key) + " "
        text = f"Led by {_names([self.label(k) for k in leaders])}."
        if c.scope == "STOCK":
            text = f"{self.label(c.key)} alone."
        if likely:
            text += " Likely to move: " + ", ".join(self.label(k) for k in likely) + "."
        stamp = view.ts(col)
        obj: dict[str, Any] = {
            "id": f"v_{stamp:%Y%m%d_%H%M}_{c.key}",
            "ts": stamp.isoformat(),
            "scope": c.scope,
            "key": c.key,
            "rvol": _r(rvol),
            "z": _r(c.z),
            "flow_share": _r(fs, 3),
            "direction": way,
            "leaders": leaders,
            "indices": likely,
            "title": f"Volume watch: {who}{times}, {words[way]}",
            "text": text,
        }
        evidence = self.evidence_line(c.scope)
        if evidence:  # no fact in the study: the key is left out, never a placeholder
            obj["evidence"] = evidence
        return obj

    def evidence_line(self, scope: str) -> str | None:
        """What the study says about such an alert; None when it has no fact for it (single-
        stock spikes were not followed in the study)."""
        facts = {f.get("id"): f for f in self.study().get("facts", [])}
        if scope != "MARKET":
            return None
        size = facts.get("size_after_market_spike", {}).get("value")
        hit = facts.get("direction_after_market_spike", {}).get("value")
        if size is None or hit is None:
            return None
        return (
            f"After such spikes NIFTY's next 15 minutes were {size:.1f} times as large as usual; "
            f"the direction followed the flow {hit * 100:.0f}% of the time."
        )

    # the order book -----------------------------------------------------
    def _read_book(self, now: dt.datetime) -> None:
        desk = self.desk
        minute = now.replace(second=0, microsecond=0)
        if desk is None or self._book_minute == minute:
            return
        if self.book_failures >= BOOK_MAX_FAILURES:
            return  # an error storm: off until the next session (reset in `_prune`)
        self._book_minute = minute
        try:
            active = bool(desk._data_api_active())
            adapter = desk._adapter_getter()
        except Exception:  # noqa: BLE001
            return
        if not active or adapter is None or not hasattr(adapter, "quote"):
            return
        from ..broker.base import Contract

        contracts = {}
        for s in self.basket:
            spec = self.series.get(s)
            if spec is None or not spec.dhan_security_id:
                continue
            contracts[str(spec.dhan_security_id)] = (
                s,
                Contract(
                    security_id=str(spec.dhan_security_id),
                    exchange_segment=spec.dhan_segment,
                    symbol=spec.label,
                    underlying=s,
                    instrument_type="EQUITY",
                ),
            )
        if not contracts:
            return
        try:
            quotes = adapter.quote([c for _, c in contracts.values()])
        except Exception as exc:  # noqa: BLE001 - the poll carries on
            self.book_failures += 1
            self.book_error = type(exc).__name__
            log.warning("volume: order book read failed (%s)", self.book_error)
            return
        self.book_failures = 0
        reading = book_reading(
            {contracts[sid][0]: q for sid, q in (quotes or {}).items() if sid in contracts}, now
        )
        if reading is None:
            return
        self.book_last = reading
        with self.engine.begin() as conn:
            conn.execute(
                insert(volume_book).values(ts=now, session=now.date().isoformat(), payload=reading)
            )

    def _prune(self, now: dt.datetime) -> None:
        today = now.date()
        if self._pruned == today:
            return
        self._pruned = today
        self.book_failures = 0  # a new session: the book may be tried again
        cut = now - dt.timedelta(days=BOOK_KEEP_DAYS)
        with self.engine.begin() as conn:
            conn.execute(delete(volume_book).where(volume_book.c.ts < cut))

    def _book_first_day(self) -> str | None:
        with self.engine.connect() as conn:
            first = conn.execute(select(func.min(volume_book.c.ts))).scalar()
        return None if first is None else f"{_utc_to_ist(first):%d %b %Y}".lstrip("0")

    # bodies -------------------------------------------------------------
    def body(self, date: str | None = None, index: str = DEFAULT_INDEX) -> dict[str, Any]:
        index = (index or DEFAULT_INDEX).upper()
        if index not in INDEX_OPTIONS:
            raise VolumeError(400, f"The index must be one of {', '.join(INDEX_OPTIONS)}.")
        if date:
            try:
                day = dt.date.fromisoformat(date)
            except ValueError as exc:
                raise VolumeError(400, "The date must look like 2026-11-24.") from exc
            return self._stored_body(day, index, requested=True)
        live = self._live_day()
        if live is not None:
            return self._live_body(live, index)
        last = self.last_stored_day()
        if last is None:
            return self._empty_body(index, ["No one-minute bars of the basket are stored yet."])
        return self._stored_body(last, index, requested=False)

    def _unknown(self, day: dt.date) -> VolumeError:
        span = self._span()
        if span is None:
            return VolumeError(
                400, f"No stored session on {day}: the store has no one-minute bars."
            )
        return VolumeError(
            400,
            f"No stored session on {day}: the store has sessions from {span[0]} to {span[1]}.",
        )

    def _stored_body(self, day: dt.date, index: str, requested: bool) -> dict[str, Any]:
        key = ("stored", day, index, self.cfg)
        with self._lock:
            hit = self._bodies.get(key)
        if hit is not None:
            return self._shown(hit, day, requested)
        bars = {s: self._stored_bars(s, day) for s in self.basket}
        if not any(v is not None for v in bars.values()):
            if requested:
                raise self._unknown(day)
            return self._empty_body(index, [f"No one-minute bars of the basket on {day}."], day)
        base = self.baselines(day, wait=False)  # never inside the request (US-097)
        if base is None:
            if day in self._build_errors:
                return self._empty_body(index, [self._build_note(day)], day)
            return self._building_body(index, day, "stored")
        view = compute_view(day, {k: v for k, v in bars.items() if v is not None}, base,
                            self.basket, self.cfg.rule)  # fmt: skip
        if view is None:
            return self._empty_body(index, [f"No one-minute bars of the basket on {day}."], day)
        alerts = [self.alert_obj(view, base, c) for c in self._pick(view, 0, [])]
        alerts.reverse()  # newest first
        idx_bars = self._stored_bars(index, day)
        body = self._assemble(view, base, index, idx_bars, alerts, source="stored")
        if day < self.clock().date():  # a complete session never changes
            with self._lock:
                self._bodies[key] = body
                while len(self._bodies) > 4 * CACHE_DAYS:
                    self._bodies.popitem(last=False)
        return self._shown(body, day, requested)

    def _shown(self, body: dict[str, Any], day: dt.date, requested: bool) -> dict[str, Any]:
        out = self._with_runtime(dict(body), live=False)
        if not requested:
            why = (
                "The desk holds no one-minute bars of today yet"
                if out["market_open"]
                else "The market is closed"
            )
            note = f"{why}: showing the last stored session, {day:%d %b %Y}."
            out["notes"] = [note.replace(" 0", " "), *out["notes"]]
        return out

    def _live_body(self, day: dt.date, index: str) -> dict[str, Any]:
        base = self.baselines(day, wait=False)
        if base is None:
            if day in self._build_errors:
                return self._empty_body(index, [self._build_note(day)], day, "live")
            return self._building_body(index, day, "live")
        view = self._view(day, base, live=True)
        if view is None:
            return self._empty_body(
                index, ["No closed one-minute bar of the basket yet today."], day, "live"
            )
        alerts = self.stored_alerts(day) if self.cfg.alerts_on else []
        idx_bars = _day_of(self._desk_bars(index), day)
        body = self._assemble(view, base, index, idx_bars, alerts, source="live")
        return self._with_runtime(body, live=True)

    def _building_body(self, index: str, day: dt.date, source: str) -> dict[str, Any]:
        """While a background thread builds the session's baselines: an empty body that says
        so, `building` true; the page asks again every 2 seconds (US-097)."""
        body = self._empty_body(index, ["Preparing this session…"], day, source)
        body["building"] = True
        return body

    def _build_note(self, day: dt.date) -> str:
        err = self._build_errors.get(day)
        if err:
            return f"The baselines for {day} could not be built ({err}); they are tried again."
        return f"The baselines for {day} are being built from the store: numbers in a moment."

    def _with_runtime(self, body: dict[str, Any], live: bool) -> dict[str, Any]:
        """The parts of a body that change with the clock: market flag, book, settings, notes
        about failures."""
        body = dict(body)
        body.setdefault("building", False)
        open_ = self._market_open()
        body["market_open"] = open_
        body["book"] = self.book_view() if (live and open_ and self.cfg.book) else None
        body["settings"] = self.settings()
        notes = list(body.get("notes") or [])
        desk_err = int(getattr(self.desk, "volume_errors", 0) or 0) if self.desk else 0
        if self.errors or desk_err:
            cls = self.last_error or getattr(self.desk, "volume_last_error", None) or "unknown"
            notes.append(f"The watch step failed {max(self.errors, desk_err)} times (last: {cls}).")
        if live and self.book_failures >= BOOK_MAX_FAILURES:
            notes.append(
                f"The order book is off until the next session after {BOOK_MAX_FAILURES} failed "
                f"reads in a row ({self.book_error})."
            )
        if not open_ and live:
            notes.insert(0, "The market is closed: this is today's session as the desk saw it.")
        body["notes"] = notes
        return body

    def settings(self) -> dict[str, Any]:
        c = self.cfg
        return {"z_alert": c.z_alert, "stock_z_alert": c.stock_z_alert,
                "cooldown_min": c.cooldown_min, "alerts_on": c.alerts_on, "notify": c.notify,
                "rule": c.rule}  # fmt: skip

    def book_view(self) -> dict[str, Any] | None:
        b = self.book_last
        if b is None:
            return None
        as_of = dt.datetime.fromisoformat(b["as_of"])
        if self.clock() - as_of > BOOK_FRESH:
            return None
        first = self._book_first_day() or f"{as_of:%d %b %Y}"
        out = dict(b)
        out["note"] = (
            f"Resting orders in the best five price steps. Recorded since {first}: not yet studied."
        )
        return out

    def _basket_block(self, view: DayView | None) -> dict[str, Any]:
        n = len(self.basket)
        return {"count": n, "label": f"{n} large stocks",
                "missing": list(view.missing) if view else list(self.basket)}  # fmt: skip

    def _empty_body(
        self,
        index: str,
        notes: list[str],
        session: dt.date | None = None,
        source: str = "stored",
    ) -> dict[str, Any]:
        now = self.clock()
        body = {
            "as_of": now.replace(second=0, microsecond=0).isoformat(),
            "session": (session or now.date()).isoformat(),
            "market_open": False,
            "source": source,
            "basket": self._basket_block(None),
            "now": None,
            "minutes": [],
            "index": {"key": index, "label": self.label(index), "minutes": []},
            "index_options": self.index_options(),
            "stocks": [],
            "indices": [],
            "alerts": [],
            "book": None,
            "settings": self.settings(),
            "notes": notes,
        }
        return self._with_runtime(body, live=False)

    def _assemble(
        self,
        view: DayView,
        base: Baselines,
        index: str,
        idx_bars: pd.DataFrame | None,
        alerts: list[dict[str, Any]],
        source: str,
    ) -> dict[str, Any]:
        lc = view.last_col
        rv = view.rv
        a5 = rv["actual_5m"]
        buy5 = ind.window_sum(view.buy_b)[0]
        cum_net = np.nancumsum(2 * view.buy_b - view.tb)
        minutes = []
        for c in range(lc + 1):
            minutes.append([
                int(view.ts(c).timestamp()), _r(view.tb[c], 0), _r(view.eb["1m"][c], 0),
                _r(view.buy_b[c], 0), _r(view.tb[c] - view.buy_b[c], 0), _r(cum_net[c], 0),
                _r(rv["rvol_5m"][c]), _r(view.zb[c]),
            ])  # fmt: skip
        fs5 = ind.flow_share(buy5[lc], a5[lc] - buy5[lc])
        buy_day = float(np.nansum(view.buy_b[: lc + 1]))
        t_day = float(np.nansum(view.tb[: lc + 1]))
        fs_day = ind.flow_share(buy_day, t_day - buy_day)
        stocks: list[dict[str, Any]] = []
        ups = downs = flats = 0
        for s in view.t:
            srv = view.stock_rv(s)
            sb5 = ind.window_sum(view.buy[s])[0][lc]
            st5 = srv["actual_5m"][lc]
            sbd = float(np.nansum(view.buy[s][: lc + 1]))
            std = float(np.nansum(view.t[s][: lc + 1]))
            prev = base.prev_close.get(s)
            last = view.last.get(s)
            chg = (last / prev - 1) * 100 if prev and last else None
            if chg is not None:
                ups, downs, flats = ups + (chg > 0), downs + (chg < 0), flats + (chg == 0)
            z = ind.finite(view.z[s][lc])
            rv5 = ind.finite(srv["rvol_5m"][lc])
            stocks.append({
                "key": s, "label": self.label(s), "last": _r(last), "chg_pct": _r(chg),
                "turnover_day": _r(std, 0), "share": _r(std / t_day, 3) if t_day > 0 else None,
                "rvol_5m": _r(rv5), "rvol_day": _r(srv["rvol_day"][lc]), "z": _r(z),
                "flow_share_5m": _r(ind.flow_share(sb5, st5 - sb5), 3),
                "flow_share_day": _r(ind.flow_share(sbd, std - sbd), 3),
                "state": ind.state(rv5, z, self.cfg.stock_z_alert, self.cfg.quiet, self.cfg.busy),
            })  # fmt: skip
        stocks.sort(key=lambda r: -(r["turnover_day"] or 0))
        indices = [self._index_row(view, base, k, trade) for k, trade in self.index_cfg]
        rv5 = ind.finite(rv["rvol_5m"][lc])
        zb = ind.finite(view.zb[lc])
        st = ind.state(rv5, zb, self.cfg.z_alert, self.cfg.quiet, self.cfg.busy)
        rvd = ind.finite(rv["rvol_day"][lc])
        times = f"{rvd:.1f} times normal" if rvd is not None else "of unknown size"
        text = f"Volume is {times} for this time of day."
        if fs5 is not None:
            side = "Buyers" if fs5 >= 0.5 else "Sellers"
            text += f" {side} {round(max(fs5, 1 - fs5) * 100)}%."
        stamp = view.ts(lc)
        notes: list[str] = []
        if view.day >= AUCTION_FROM and lc >= 359:
            notes.append("Stock bars end at 15:14 since the closing auction began.")
        if view.no_history:
            notes.append(
                "Left out of the totals for want of history: "
                + ", ".join(self.label(s) for s in view.no_history) + "."
            )  # fmt: skip
        if self.desk is not None and int(getattr(self.desk, "fine_minutes", 1) or 1) != 1:
            notes.append(
                "The desk's bars are 2-minute (Yahoo): the live view needs one-minute bars, so "
                "a stored session is shown."
            )
        idx_rows = []
        if idx_bars is not None:
            for ts, c in idx_bars["close"].items():
                idx_rows.append([int(pd.Timestamp(ts).timestamp()), _r(c)])
        return {
            "as_of": (stamp + dt.timedelta(minutes=1)).isoformat(),
            "session": view.day.isoformat(),
            "market_open": False,
            "source": source,
            "basket": self._basket_block(view),
            "now": {
                "minute": f"{stamp:%H:%M}",
                "turnover_1m": _r(view.tb[lc], 0),
                "expected_1m": _r(view.eb["1m"][lc], 0),
                "rvol_1m": _r(rv["rvol_1m"][lc]),
                "rvol_5m": _r(rv5),
                "rvol_day": _r(rvd),
                "z": _r(zb),
                "state": st,
                "flow_share_5m": _r(fs5, 3),
                "flow_share_day": _r(fs_day, 3),
                "turnover_day": _r(t_day, 0),
                "expected_day": _r(rv["expected_day"][lc], 0),
                "net_flow_day": _r(cum_net[lc], 0),
                "up": ups,
                "down": downs,
                "flat": flats,
                "text": text,
            },
            "minutes": minutes,
            "index": {"key": index, "label": self.label(index), "minutes": idx_rows},
            "index_options": self.index_options(),
            "stocks": stocks,
            "indices": indices,
            "alerts": alerts,
            "book": None,
            "settings": self.settings(),
            "notes": notes,
        }

    def _index_row(self, view: DayView, base: Baselines, key: str, trade: bool) -> dict:
        w, r2 = base.weights.get(key, ({}, None))
        lc = view.last_col
        net5, e5, netd, ed = {}, {}, {}, {}
        for s in view.keys:
            n = 2 * view.buy[s] - view.t[s]
            net5[s] = view.win(n, lc)
            e5[s] = float(view.exp[s]["5m"][lc])
            netd[s] = float(np.nansum(n[: lc + 1]))
            ed[s] = float(view.exp[s]["day"][lc])
        p5 = ind.pressure(w, net5, e5)
        pd_ = ind.pressure(w, netd, ed)
        top = sorted(((s, v) for s, v in w.items() if v > 0), key=lambda x: -x[1])[:3]
        if p5 is None or abs(p5) < PRESSURE_WORDS:
            text = "No clear pressure."
        else:
            contrib = {s: w.get(s, 0.0) * net5[s] / e5[s] for s in view.keys if e5.get(s)}
            sign = 1 if p5 > 0 else -1
            lead = [s for s in sorted(contrib, key=lambda k: -sign * contrib[k])
                    if sign * contrib[s] > 0][:2]  # fmt: skip
            word = "Buying" if p5 > 0 else "Selling"
            text = f"{word} pressure" + (
                f", led by {_names([self.label(s) for s in lead])}." if lead else "."
            )
        return {
            "key": key, "label": self.label(key), "trade": trade, "pressure_5m": _r(p5),
            "pressure_day": _r(pd_), "r2": _r(r2), "top": [[s, _r(v)] for s, v in top],
            "text": text,
        }  # fmt: skip

    # the other routes ---------------------------------------------------
    def profile(self) -> dict[str, Any]:
        day = self._live_day() or self.last_stored_day()
        empty: dict[str, Any] = {"as_of": None, "sessions": 0, "minutes": [], "half_hours": [], "busiest": [],
                 "quietest": [], "weekdays": [], "expiry": None}  # fmt: skip
        if day is None:
            return empty | {"text": "No one-minute bars of the basket are stored yet."}
        base = self.baselines(day, wait=False)
        if base is None:
            return empty | {
                "text": "The profile is being built from the store: try again in a moment."
            }
        keys = [s for s in self.basket if s in base.hist]
        if not keys:
            return empty | {"text": "No history of the basket is stored yet."}
        b = base.basket(keys)
        full_rows = b["full"].to_numpy()
        tb = b["tb"].loc[full_rows].iloc[-ind.Z_LOOKBACK :]
        prof = ind.profile(tb)
        rv = ind.day_rvol(tb, base.expected_hist(b["keys"], b["tb"].index).reindex(tb.index))
        expiry = ind.nifty_expiry_days(b["tb"].index)
        is_exp = np.array([d in expiry for d in tb.index])
        rvv = rv.to_numpy()
        e = ind.finite(np.nanmean(rvv[is_exp])) if is_exp.any() else None
        o = ind.finite(np.nanmean(rvv[~is_exp])) if (~is_exp).any() else None
        first = prof.get("first_share") or 0.0
        qh = prof.get("quiet_hour")
        text = f"The first half hour carries about {_share_words(first)} of the day's volume"
        if qh:
            end = dt.datetime.strptime(qh[1], "%H:%M") + dt.timedelta(minutes=1)
            text += f"; {qh[0]} to {end:%H:%M} is the quietest hour."
        else:
            text += "."
        return {
            "as_of": tb.index[-1].date().isoformat() if len(tb) else None,
            "sessions": prof["sessions"],
            "minutes": [[m, _r(a, 0), _r(b_, 0), _r(c, 0)] for m, a, b_, c in prof["minutes"]],
            "half_hours": prof["half_hours"],
            "busiest": prof["busiest"],
            "quietest": prof["quietest"],
            "weekdays": [{"day": w["day"], "rvol": w["rvol"]} for w in ind.weekday_rvol(rv)],
            "expiry": {
                "expiry_day_rvol": _r(e),
                "other_days_rvol": _r(o),
                "note": "NIFTY expiry days",
            },  # fmt: skip
            "text": text,
        }

    def alerts(self, limit: int = 100) -> dict[str, Any]:
        q = (
            select(volume_alerts.c.payload, volume_alerts.c.session)
            .order_by(volume_alerts.c.ts.desc(), volume_alerts.c.id)
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return {"alerts": [dict(r.payload) | {"session": r.session} for r in conn.execute(q)]}

    def study(self) -> dict[str, Any]:
        try:
            return dict(json.loads(EVIDENCE_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {"as_of": None, "note": None, "facts": [], "alerts_per_day": None}


def _share_words(x: float) -> str:
    for frac, words in ((1 / 5, "a fifth"), (1 / 4, "a quarter"), (1 / 6, "a sixth"),
                        (1 / 3, "a third"), (1 / 8, "an eighth")):  # fmt: skip
        if abs(x - frac) <= 0.012:
            return words
    return f"{x * 100:.0f}%"


def _col(ts: str) -> int:
    t = dt.datetime.fromisoformat(ts).astimezone(IST)
    return t.hour * 60 + t.minute - ind.OPEN_MINUTE


def book_reading(quotes: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
    """The basket's order book from quotes with five steps a side: per stock the resting
    quantity on each side, the bid share and the spread in basis points of the mid."""
    rows = []
    tb = ta = 0.0
    for key, q in sorted(quotes.items()):
        bids = tuple(getattr(q, "bids", ()) or ())
        asks = tuple(getattr(q, "asks", ()) or ())
        if not bids or not asks:
            continue
        bq = float(sum(x[1] for x in bids))
        aq = float(sum(x[1] for x in asks))
        b1, a1 = bids[0][0], asks[0][0]
        mid = (a1 + b1) / 2
        spread = (a1 - b1) / mid * 1e4 if mid > 0 and a1 >= b1 else None
        rows.append({"key": key, "bid_qty": int(bq), "ask_qty": int(aq),
                     "bid_share": _r(bq / (bq + aq), 3) if bq + aq > 0 else None,
                     "spread_bps": _r(spread)})  # fmt: skip
        tb, ta = tb + bq * mid, ta + aq * mid
    if not rows:
        return None
    return {
        "as_of": now.replace(microsecond=0).isoformat(),
        "bid_share": _r(tb / (tb + ta), 3) if tb + ta > 0 else None,
        "stocks": rows,
    }
