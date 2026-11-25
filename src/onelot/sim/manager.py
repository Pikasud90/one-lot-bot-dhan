"""The Simulator's sessions by id: created, kept in memory, rebuilt from `sim_sessions` at
their cursor when not in memory (a restart, a new desk). One manager per running desk
(`routes.register`), the way `api/server.py` keeps the Machine Learning Lab."""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine

from ..clock import IST, now_ist
from ..config import CostConfig
from ..costs import CostModel
from ..data.resample import minutes_of
from ..data.universe import InstrumentSpec, load_specs
from ..desk.config import DeskInstrumentCfg, load_desk_config
from ..settings import current_settings
from ..strategies.registry import REGISTRY, SUITABLE
from .bars import MIN_DAY_BARS, BarSource, DeskBars, StoreBars, day_number, day_start
from .candles import (
    CHART_TIMEFRAMES,
    COUNT_DEFAULT,
    COUNT_MAX,
    COUNT_MIN,
    calendar_days_for,
    candles_to,
)
from .engine import (
    ATR_TIMEFRAMES,
    DEFAULTS,
    MAX_CANDLES,
    MAX_EVENTS,
    SESSION_DEFAULTS,
    TRAINING,
    Deps,
    Frames,
    Setup,
    SimError,
    SimSession,
    _hhmm_min,
    _iso,
    _ns_of,
    base_premium_note,
    closed_row,
    lot_unit_of,
    session_view_of,
    summary_of,
    window_at,
)
from .frames import FrameCache, Window
from .plots import plot_specs
from .pricing import open_option_store
from .store import SimStore
from .worker import Builder, Job

#: the timeframes a single-timeframe strategy can be simulated on
TIMEFRAMES: tuple[str, ...] = ("1m", "3m", "5m", "10m", "15m", "30m", "60m")
#: what a session's numbers may be: (lowest, highest, how the sentence says it)
LIMITS: dict[str, tuple[float, float, str]] = {
    "stop_atr_k": (0.1, 10.0, "0.1 to 10"),
    "target_r": (0.5, 20.0, "0.5 to 20"),
    "capital": (1000.0, 1e9, "1,000 to 1,000,000,000 rupees"),
}
SPEEDS: tuple[int, ...] = (1, 2, 5, 10, 60, 300)
#: what a strategy's `SUITABLE` categories mean for the desk's two kinds
_KIND_OF = {"OPTIONS": "INDEX", "STOCK": "STOCK"}


#: "thread": builds run in the worker (the app); "inline": in the request (the tests)
BUILD_MODE = "thread"
#: built sessions kept in memory; an older one is rebuilt from its rows when asked for
MAX_SESSIONS = 4
BUILDING_TEXT = "The session is still being prepared."
REPLACED = "A newer choice replaced this session before it was ready."
#: the choice the page starts with (SIM-API-2.md)
DEFAULT_CHOICE: dict[str, str] = {
    "instrument": "NIFTY",
    "strategy": "microtrend",
    "timeframe": "3m",
}


def _plain(exc: BaseException) -> str:
    """A build's failure in one plain sentence."""
    if isinstance(exc, SimError):
        return exc.message
    text = str(exc).strip().rstrip(".") or type(exc).__name__
    return f"The session could not be prepared: {text}."


def strategy_kinds(name: str) -> list[str]:
    cats = SUITABLE.get(name, ())
    return [k for c, k in _KIND_OF.items() if c in cats]


def strategy_timeframes(name: str) -> tuple[list[str], str]:
    cls = REGISTRY[name]
    default = str(cls.Params.model_fields["timeframe"].default)
    if cls.multi_timeframe:
        return [default], default
    tfs = list(TIMEFRAMES)
    if default not in tfs:
        tfs.append(default)
    return tfs, default


def _ts(value: Any) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)


def default_deps(
    desk: Any = None,
    option_store_path: str | Path | None = None,
    clock: Callable[[], dt.datetime] = now_ist,
) -> Deps:
    s = current_settings()
    costs = CostConfig.load(s.project_root / "config" / "costs.yaml")
    path = option_store_path or (s.project_root / "data" / "expired_options.db")
    live = getattr(desk, "real_premium_at", None) if desk is not None else None
    return Deps(
        bt=s.backtest,
        stock_costs=CostModel(costs.product("equity_intraday")),
        option_costs=CostModel(costs.product("index_options")),
        option_store=open_option_store(path),
        live_premium=live,
        security_id_for=_chain_lookup(desk) if desk is not None else None,
        clock=clock,
    )


def _chain_lookup(desk: Any) -> Callable[[str, float, str], str | None]:
    """The desk's cached option chain row's security id for a strike (read, never fetched)."""

    def find(key: str, strike: float, opt: str) -> str | None:
        cache = getattr(desk, "_chain_cache", None) or {}
        hit = cache.get(key)
        chain = hit[1] if hit else None
        for r in getattr(chain, "rows", None) or []:
            if r.opt_type == opt and abs(float(r.strike) - strike) < 1e-6 and r.security_id:
                return str(r.security_id)
        return None

    return find


class Entry:
    """A session the manager knows: BUILDING (no `session` yet), FAILED (`detail`), or one
    whose `session` answers (its own status). One lock per session (SIM-03: not one for the
    manager)."""

    def __init__(self, setup: Setup, cursor: str, status: str = "BUILDING") -> None:
        self.setup = setup
        self.cursor = cursor
        self.status = status
        self.session: SimSession | None = None
        self.detail: str | None = None
        self.job: Job | None = None
        self.lock = threading.RLock()
        self.row: dict[str, Any] | None = None  # the stored row, for a session being rebuilt
        self.started = time.monotonic()

    @property
    def state_status(self) -> str:
        return self.session.status if self.session is not None else self.status


class SimManager:
    def __init__(
        self,
        engine: Engine,
        desk: Any = None,
        deps: Deps | None = None,
        bars: BarSource | None = None,
        clock: Callable[[], dt.datetime] = now_ist,
        instruments: tuple[DeskInstrumentCfg, ...] | None = None,
        build: str | None = None,
        max_sessions: int | None = None,
    ) -> None:
        self.engine = engine
        self.desk = desk
        self.clock = clock
        self.deps = deps or default_deps(desk, clock=clock)
        self.store = SimStore(engine)
        self.store_bars: BarSource = bars or StoreBars(engine)
        self.series, self.specs = load_specs()
        self.instruments = instruments or load_desk_config().instruments
        self.sessions: OrderedDict[str, Entry] = OrderedDict()
        self.max_sessions = max_sessions or MAX_SESSIONS
        self.lock = threading.RLock()  # the session table only, never held during work
        self.frame_cache = FrameCache()
        self.frames = Frames(self.frame_cache, self._prefetch)
        mode = build or BUILD_MODE
        if mode not in ("thread", "inline"):
            raise ValueError(f"build must be thread or inline, not {mode!r}")
        self.builder = Builder(inline=mode == "inline")

    # ── what can be simulated ─────────────────────────────────────────────
    def _live_keys(self) -> set[str]:
        trackers = getattr(self.desk, "trackers", None) or {}
        return set(trackers)

    def _range(self, key: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        try:
            return self.store_bars.minute_range(key)
        except Exception:  # noqa: BLE001 - an instrument without bars lists no range
            return None

    def _spec_of(self, inst: DeskInstrumentCfg) -> InstrumentSpec | None:
        return self.specs.get(inst.tracked or inst.key) or self.specs.get(inst.key)

    def options(self) -> dict[str, Any]:
        """The instruments with their first and last stored minute (`series_spans`, kept
        until bars are written: never a count), the strategies with their lines, the
        defaults."""
        live = self._live_keys()
        rows = []
        for inst in self.instruments:
            rng = self._range(inst.key)
            series = self.series.get(inst.key)
            spec = self._spec_of(inst)
            lot, unit = lot_unit_of(spec, inst.kind) if spec is not None else (1, "share")
            rows.append(
                {
                    "key": inst.key,
                    "label": series.label if series is not None else inst.key,
                    "kind": inst.kind,
                    "first": rng[0].isoformat() if rng else None,
                    "last": rng[1].isoformat() if rng else None,
                    "live": inst.key in live,
                    "trade": inst.key not in TRAINING,
                    "tick": float(spec.tick_size) if spec is not None else 0.05,
                    "lot": lot,
                    "unit": unit,
                }
            )
        strategies = []
        for name, cls in REGISTRY.items():
            tfs, default = strategy_timeframes(name)
            strategies.append(
                {
                    "name": name,
                    "label": cls.label,
                    "kinds": strategy_kinds(name),
                    "timeframes": tfs,
                    "default_timeframe": default,
                    "description": cls.description,
                    "plots": plot_specs(name),
                }
            )
        return {
            "instruments": rows,
            "strategies": strategies,
            "speeds": list(SPEEDS),
            "chart_timeframes": list(CHART_TIMEFRAMES),
            "defaults": {
                "capital": SESSION_DEFAULTS["capital"],
                "stop_atr_k": DEFAULTS["stop_atr_k"],
                "target_r": DEFAULTS["target_r"],
                "extend": DEFAULTS["extend"],
                "stop_atr_tf": DEFAULTS["stop_atr_tf"],
                **DEFAULT_CHOICE,
            },
        }

    def days(self, instrument: Any) -> dict[str, Any]:
        """`GET /api/sim/days`: the stored trading days (at least 300 one-minute bars)."""
        inst = self._instrument(instrument)
        days_of = getattr(self.store_bars, "days", None)
        if days_of is not None:
            days = list(days_of(inst.key))
        else:  # a bar source without its own list (the tests' bars in memory)
            m = self.store_bars.minute_bars(inst.key, None, None)
            if m.empty:
                days = []
            else:
                local = pd.DatetimeIndex(m.index).tz_convert(IST).strftime("%Y-%m-%d")
                counts = pd.Series(1, index=local).groupby(level=0).sum()
                days = [d for d, n in counts.items() if int(n) >= MIN_DAY_BARS]
        rng = self._range(inst.key)
        return {
            "instrument": inst.key,
            "days": days,
            "first": rng[0].isoformat() if rng else None,
            "last": rng[1].isoformat() if rng else None,
        }

    def candles(
        self, instrument: Any, timeframe: Any = "3m", until: Any = None, count: Any = None
    ) -> dict[str, Any]:
        """`GET /api/sim/candles`: closed candles to the minute `until`, no session."""
        inst = self._instrument(instrument)
        tf = str(timeframe or "3m")
        if tf not in CHART_TIMEFRAMES:
            raise SimError(400, f"The timeframe must be one of {', '.join(CHART_TIMEFRAMES)}.")
        n = COUNT_DEFAULT if count in (None, "") else count
        try:
            if isinstance(n, bool) or float(n) != int(float(n)):
                raise ValueError(n)
            n = int(float(n))
        except (TypeError, ValueError):
            raise SimError(400, "count must be a whole number from 50 to 1000.") from None
        if not COUNT_MIN <= n <= COUNT_MAX:
            raise SimError(400, "count must be a whole number from 50 to 1000.")
        spec = self._spec_of(inst)
        series = self.series.get(inst.key)
        meta = {
            "key": inst.key,
            "label": series.label if series is not None else inst.key,
            "kind": inst.kind,
            "tick": float(spec.tick_size) if spec is not None else 0.05,
        }
        out: dict[str, Any] = {
            "instrument": meta,
            "timeframe": tf,
            "tf_minutes": minutes_of(tf),
            "until": None,
            "candles": [],
            "forming": None,
        }
        rng = self._range(inst.key)
        if rng is None:
            out["note"] = f"There are no stored one-minute bars of {inst.key}."
            return out
        if until in (None, ""):
            u = rng[1]
        else:
            try:
                u = _ts(until)
            except (TypeError, ValueError):
                raise SimError(400, "until is not a date and time.") from None
            if u < rng[0]:
                out["note"] = (
                    f"The stored bars of {inst.key} begin at {rng[0]:%Y-%m-%d %H:%M}: "
                    "the candles end there."
                )
                u = rng[0]
            elif u > rng[1] + pd.Timedelta(minutes=1):
                out["note"] = (
                    f"The stored bars of {inst.key} end at {rng[1]:%Y-%m-%d %H:%M}: "
                    "the candles end there."
                )
                u = rng[1]
        u = u.floor("min")
        close_min = _hhmm_min((spec.session if spec is not None else {}).get("close", "15:30"))
        rows, forming, _ = self._candles_to(inst.key, tf, close_min, u, n, rng[0])
        out.update(until=u.isoformat(), candles=rows, forming=forming)
        return out

    def _candles_to(
        self,
        key: str,
        tf: str,
        close_min: int,
        until: pd.Timestamp,
        count: int,
        first: pd.Timestamp,
    ) -> tuple[list[list], list | None, int | None]:
        """(closed candles, forming candle, the start of the last stored minute up to `until`)
        from one bounded read; read further back only when the window held too few candles."""
        cal = calendar_days_for(count, minutes_of(tf))
        until_ns = int(until.value)
        while True:
            lo = day_start(day_number(until) - cal)
            m = self.store_bars.minute_bars(key, lo, until + pd.Timedelta(minutes=1))
            rows, forming, n = candles_to(m, tf, close_min, until_ns, count)
            if n >= count or lo <= first or cal > 4000:
                last = int(pd.Timestamp(m.index[-1]).value) if not m.empty else None
                return rows, forming, last
            cal *= 2

    # ── sessions ──────────────────────────────────────────────────────────
    def _instrument(self, key: Any) -> DeskInstrumentCfg:
        k = str(key or "").upper()
        for inst in self.instruments:
            if inst.key == k:
                return inst
        names = ", ".join(i.key for i in self.instruments)
        raise SimError(400, f"Unknown instrument {key!r}: choose one of {names}.")

    def _bars(self, mode: str) -> BarSource:
        if mode == "live":
            return DeskBars(self.desk, self.clock, self.store_bars)
        return self.store_bars

    def _setup(self, body: dict[str, Any], sid: str) -> Setup:
        inst = self._instrument(body.get("instrument"))
        name = str(body.get("strategy") or "")
        if name not in REGISTRY:
            raise SimError(400, f"Unknown strategy {name!r}.")
        kinds = strategy_kinds(name)
        if inst.kind not in kinds:
            raise SimError(
                400, f"The strategy {name} is not meant for {inst.kind.lower()} instruments."
            )
        tfs, default = strategy_timeframes(name)
        tf = str(body.get("timeframe") or default)
        if tf not in tfs:
            raise SimError(400, f"The strategy {name} runs on {', '.join(tfs)}, not {tf!r}.")
        mode = str(body.get("mode") or "past")
        if mode not in ("past", "live"):
            raise SimError(400, "The mode must be past or live.")
        params = {**DEFAULTS, **SESSION_DEFAULTS}
        given = body.get("params") or {}
        if not isinstance(given, dict):
            raise SimError(400, "The params must be an object.")
        for k in params:
            if k in given and given[k] is not None:
                params[k] = given[k]
        for k, (lo, hi, label) in LIMITS.items():
            try:
                v = params[k]
                if isinstance(v, bool) or isinstance(v, str):
                    raise ValueError(k)
                v = float(v)
            except (TypeError, ValueError):
                raise SimError(400, f"{k} must be a number ({label}).") from None
            if not math.isfinite(v) or not lo <= v <= hi:
                raise SimError(400, f"{k} must be a number ({label}).")
            params[k] = v
        params["stop_atr_tf"] = str(params["stop_atr_tf"])
        if params["stop_atr_tf"] not in ATR_TIMEFRAMES:
            raise SimError(400, "stop_atr_tf must be 60m or own.")
        for k in ("extend", "auto_exit"):
            if not isinstance(params[k], bool):
                raise SimError(400, f"{k} must be true or false.")
        if mode == "live":
            if self.desk is None:
                raise SimError(400, "The live desk is not running, so there is no live feed.")
            if inst.key not in self._live_keys():
                raise SimError(400, f"The live desk has no feed for {inst.key}.")
            start = _ts(self.clock()).floor("min")
        else:
            if not body.get("start"):
                raise SimError(400, "A past session needs a start time.")
            try:
                start = _ts(body["start"])
            except (TypeError, ValueError):
                raise SimError(400, "The start is not a date and time.") from None
            rng = self._range(inst.key)
            if rng is None:
                raise SimError(400, f"There are no stored one-minute bars of {inst.key}.")
            if start < rng[0] or start > rng[1]:
                raise SimError(
                    400,
                    f"The start is outside the stored data of {inst.key}: "
                    f"{rng[0]:%Y-%m-%d %H:%M} to {rng[1]:%Y-%m-%d %H:%M}.",
                )
            if start == rng[0]:
                raise SimError(
                    400,
                    f"A session needs one stored minute before its start: {inst.key} begins at "
                    f"{rng[0]:%Y-%m-%d %H:%M}, so start a minute or more after that.",
                )
        tracked = inst.tracked or inst.key
        spec = self.specs.get(tracked) or self.specs.get(inst.key)
        if spec is None:
            raise SimError(400, f"{inst.key} has no instrument in config/universe.yaml.")
        series = self.series.get(inst.key)
        return Setup(
            sid,
            inst.key,
            series.label if series is not None else inst.key,
            inst.kind,
            inst.key,
            spec,
            series,
            name,
            tf,
            mode,
            start,
            params,
        )

    # ── building: off the request ─────────────────────────────────────────
    def _frame_job_key(self, setup: Setup, bars: BarSource, window: Window) -> tuple:
        key = self.frames.key(setup, bars, window)
        return ("frame", key) if key is not None else ("session", setup.id)

    def _prefetch(self, setup: Setup, bars: BarSource, window: Window) -> None:
        """A past session's next window, prepared in the worker (`SimSession._maybe_prefetch`)."""
        if self.frames.ready(setup, bars, window):
            return
        self.builder.submit(
            self._frame_job_key(setup, bars, window),
            lambda job: self.frames.frame(setup, bars, window, job.set_step),
            "prefetch",
        )

    def prepare(self, query: dict[str, Any]) -> dict[str, Any]:
        """`GET /api/sim/prepare`: warm the caches of a choice; no session, nothing written."""
        body = {**query, "mode": "past"}
        inst = self._instrument(body.get("instrument"))
        if not body.get("start"):
            rng = self._range(inst.key)
            if rng is None:
                raise SimError(400, f"There are no stored one-minute bars of {inst.key}.")
            body["start"] = rng[1].isoformat()
        setup = self._setup(body, "s_prepare")
        name = f"{setup.instrument_key}|{setup.strategy}|{setup.timeframe}"
        bars = self._bars("past")
        window = window_at(setup.strategy, _ns_of(setup.start))
        key = self.frames.key(setup, bars, window)
        if key is None:
            return {"key": name, "status": "ready", "ms": 0}  # nothing is kept for these bars
        hit = self.frame_cache.peek(key)
        if hit is not None:
            return {"key": name, "status": "ready", "ms": round(hit.build_ms)}
        job = self.builder.submit(
            ("frame", key),
            lambda job: self.frames.frame(setup, bars, window, job.set_step),
            "prepare",
        )
        hit = self.frame_cache.peek(key)
        if hit is not None:
            return {"key": name, "status": "ready", "ms": round(hit.build_ms)}
        if job.done.is_set() and job.error is not None:
            raise SimError(400, _plain(job.error))
        return {"key": name, "status": "building", "ms": round(job.elapsed_ms())}

    def _new_session(
        self, entry: Entry, restore: dict[str, Any] | None, step: Callable[[str], None] | None
    ) -> SimSession:
        setup = entry.setup
        return SimSession(
            setup, self.deps, self._bars(setup.mode), self.store, restore, self.frames, step
        )

    def _run_build(self, entry: Entry, restore: dict[str, Any] | None) -> None:
        """Build the session of `entry` now, in this thread (its frame is ready, or this is
        the worker)."""

        def step(name: str) -> None:
            job = entry.job
            if job is not None:
                job.set_step(name)

        try:
            session = self._new_session(entry, restore, step)
        except Exception as exc:  # noqa: BLE001 - the page is told in plain words
            self._fail(entry, _plain(exc))
            if isinstance(exc, SimError) and self.builder.inline:
                raise
            return
        with entry.lock:
            entry.session = session
            entry.status = session.status
            entry.row = None
            entry.cursor = session.cursor
        if restore is None:
            session.save()  # the cursor of the first minute and the RUNNING status
        self._evict()

    def _fail(self, entry: Entry, detail: str) -> None:
        with entry.lock:
            entry.status = "FAILED"
            entry.detail = detail
        try:
            row = self.store.session(entry.setup.id)
            if row is not None and row.get("status") in ("BUILDING", None):
                params = dict(row.get("params") or {})
                params["_failed"] = detail
                self.store.save_session(entry.setup.id, row["cursor_ts"], "FAILED", params)
        except Exception:  # noqa: BLE001 - the answer in memory stands
            pass

    def _schedule(self, entry: Entry, restore: dict[str, Any] | None) -> None:
        """Build at once when the frame is ready (or in inline mode); else in the worker, and
        the entry answers BUILDING until then."""
        setup = entry.setup
        bars = self._bars(setup.mode)
        if restore is not None:
            st = (restore.get("params") or {}).get("_state") or {}
            at = _ns_of(restore["cursor_ts"]) + 60 * 1_000_000_000
            win = st.get("window")
            window = (
                Window(*(None if v is None else _ts(pd.Timestamp(int(v), tz="UTC")) for v in win))
                if win and len(win) == 3
                else window_at(setup.strategy, at)
            )
        else:
            window = window_at(setup.strategy, _ns_of(setup.start))
        ready = setup.mode == "past" and self.frames.ready(setup, bars, window)
        if ready or self.builder.inline:
            self._run_build(entry, restore)
            return
        if setup.mode == "live" or self.frames.key(setup, bars, window) is None:
            # nothing to keep for these bars: the whole build runs in `_run_build`
            key: tuple = ("session", setup.id)

            def build(job: Job) -> Any:
                return None

        else:
            key = self._frame_job_key(setup, bars, window)

            def build(job: Job) -> Any:
                return self.frames.frame(setup, bars, window, job.set_step)

        job = self.builder.submit(key, build, "create")
        entry.job = job

        def done(j: Job) -> None:
            if j.replaced or j.dropped:
                self._fail(entry, REPLACED)
            elif j.error is not None:
                self._fail(entry, _plain(j.error))
            else:
                self._run_build(entry, restore)

        job.then(done)

    def _remember(self, entry: Entry) -> None:
        with self.lock:
            self.sessions[entry.setup.id] = entry
            self.sessions.move_to_end(entry.setup.id)

    def _evict(self) -> None:
        """At most `max_sessions` built sessions in memory: the least recently used ones are
        saved and dropped (they are rebuilt from their rows when asked for)."""
        with self.lock:
            built = [e for e in self.sessions.values() if e.session is not None]
            extra = len(built) - self.max_sessions
            victims = []
            for e in built:
                if extra <= 0:
                    break
                victims.append(e)
                extra -= 1
        for e in victims:
            if not e.lock.acquire(blocking=False):
                continue  # in use right now: it stays
            try:
                if e.session is not None:
                    e.session.save()
                with self.lock:
                    self.sessions.pop(e.setup.id, None)
            finally:
                e.lock.release()

    def _cursor_before(self, setup: Setup) -> tuple[str, list[list]]:
        """(the minute before the start as an ISO time, up to 300 closed candles to it): the
        cheap read a BUILDING answer shows."""
        start = setup.start
        spec = setup.spec
        close_min = _hhmm_min(spec.session.get("close", "15:30"))
        rng = self._range(setup.series_key)
        first = rng[0] if rng is not None else start
        until = (start - pd.Timedelta(minutes=1)).floor("min")
        rows, _, last = self._candles_to(
            setup.series_key, setup.timeframe, close_min, until, MAX_CANDLES, first
        )
        cursor = _iso(last) if last is not None else until.isoformat()
        return str(cursor), rows

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /api/sim/sessions`: answers at once. A choice whose frame is ready (or in
        inline mode) is built here and answers RUNNING; any other answers BUILDING with the
        cheap candles, and the worker builds it."""
        sid = f"s_{secrets.token_hex(4)}"
        setup = self._setup(body, sid)
        row = {
            "id": sid,
            "created_at": setup.created_at,
            "instrument_key": setup.instrument_key,
            "strategy": setup.strategy,
            "timeframe": setup.timeframe,
            "mode": setup.mode,
            "start_ts": setup.start.to_pydatetime(),
            "status": "BUILDING",
        }
        bars = self._bars(setup.mode)
        window = window_at(setup.strategy, _ns_of(setup.start))
        ready = setup.mode == "past" and self.frames.ready(setup, bars, window)
        if ready or self.builder.inline:
            entry = Entry(setup, setup.start.isoformat())
            session = self._new_session(entry, None, None)  # a SimError is the 400, as before
            row.update(
                cursor_ts=pd.Timestamp(session.cursor_ns, tz="UTC").to_pydatetime(),
                status=session.status,
                params=session._params_json(),
            )
            self.store.add_session(row)
            session.saved_revealed = session.revealed
            entry.session, entry.status, entry.cursor = session, session.status, session.cursor
            self._remember(entry)
            self._evict()
            return self._created(entry, [])
        if setup.mode == "past":
            cursor, candles = self._cursor_before(setup)
        else:
            cursor, candles = setup.start.isoformat(), []
        entry = Entry(setup, cursor)
        row.update(cursor_ts=_ts(cursor).to_pydatetime(), params={**setup.params, "_state": {}})
        self.store.add_session(row)
        self._remember(entry)
        self._schedule(entry, None)
        return self._created(entry, candles)

    def _created(self, entry: Entry, candles: list[list]) -> dict[str, Any]:
        with entry.lock:
            s = entry.session
            if s is not None:
                read = s.read()
                state = {k: v for k, v in read.items() if k != "candles"}
                return {
                    "session": state["session"],
                    "candles": read["candles"],
                    "state": state,
                    "events": state["events"],
                    "overlays": state["overlays"],
                    "marks": state["marks"],
                }
            view = self._pending(entry, candles)
        state = {k: v for k, v in view.items() if k != "candles"}
        out = {
            "session": state["session"],
            "candles": candles,
            "state": state,
            "events": state["events"],
            "overlays": state["overlays"],
            "marks": state["marks"],
        }
        if "build" in state:
            out["build"] = state["build"]
        if "detail" in state:
            out["detail"] = state["detail"]
        return out

    def _pending(self, entry: Entry, candles: list[list] | None = None) -> dict[str, Any]:
        """The read of a session that is BUILDING or FAILED: what its row and the cheap reads
        hold (never a rebuild: F5)."""
        setup = entry.setup
        if candles is None:
            if setup.mode == "past":
                close_min = _hhmm_min(setup.spec.session.get("close", "15:30"))
                rng = self._range(setup.series_key)
                first = rng[0] if rng is not None else setup.start
                candles, _, _ = self._candles_to(
                    setup.series_key, setup.timeframe, close_min, _ts(entry.cursor),
                    MAX_CANDLES, first,
                )  # fmt: skip
            else:
                candles = []
        closed: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        if entry.row is not None:
            closed = [
                closed_row(r) for r in self.store.trades(session_id=setup.id, status="CLOSED")
            ]
            events = self.store.events(setup.id, MAX_EVENTS)
        note = base_premium_note(setup, self.deps)
        out: dict[str, Any] = {
            "session": session_view_of(setup, entry.cursor, entry.status, note),
            "alert": None,
            "trade": None,
            "summary": summary_of(closed, float(setup.params["capital"])),
            "trades": closed,
            "candles": candles,
            "events": events,
            "overlays": {},
            "marks": [],
        }
        if entry.status == "BUILDING":
            job = entry.job
            out["build"] = {
                "step": job.step if job is not None else "reading bars",
                "elapsed_ms": round(
                    job.elapsed_ms()
                    if job is not None
                    else (time.monotonic() - entry.started) * 1000
                ),
            }
        elif entry.status == "FAILED":
            out["detail"] = entry.detail or "The session could not be prepared."
        return out

    def entry(self, sid: str) -> Entry:
        """The session's entry; one that is not in memory is answered from its stored row and
        rebuilt (at once when its frame is ready, else in the worker)."""
        with self.lock:
            hit = self.sessions.get(sid)
            if hit is not None:
                self.sessions.move_to_end(sid)
                return hit
        row = self.store.session(sid)
        if row is None:
            raise SimError(404, f"There is no simulator session {sid!r}.")
        if row["mode"] == "live" and self.desk is None:
            raise SimError(409, "A live session needs the running desk.")
        body = {
            "instrument": row["instrument_key"],
            "params": {k: v for k, v in (row["params"] or {}).items() if not k.startswith("_")},
        }
        setup = self._setup_stored(body, row)
        entry = Entry(setup, _ts(row["cursor_ts"]).isoformat())
        entry.row = row
        if row.get("status") == "FAILED":
            entry.status = "FAILED"
            entry.detail = str((row.get("params") or {}).get("_failed") or "") or None
        with self.lock:
            hit = self.sessions.get(sid)
            if hit is not None:
                return hit
            self.sessions[sid] = entry
        if entry.status != "FAILED":
            try:
                self._schedule(entry, row)
            except SimError:
                pass  # FAILED, said in the entry
        return entry

    def get(self, sid: str) -> SimSession:
        """The built session (waits for a build in the worker: the tests and scripts use it)."""
        entry = self.entry(sid)
        job = entry.job
        if entry.session is None and job is not None:
            job.done.wait(60.0)
            self.builder.idle(60.0)
        if entry.session is None:
            if entry.status == "FAILED":
                raise SimError(409, entry.detail or "The session could not be prepared.")
            raise SimError(409, BUILDING_TEXT)
        return entry.session

    def read(self, sid: str) -> dict[str, Any]:
        """`GET /api/sim/sessions/{id}`."""
        entry = self.entry(sid)
        with entry.lock:
            if entry.session is not None:
                return entry.session.read()
            return self._pending(entry)

    def act(self, sid: str, fn: Callable[[SimSession], Any]) -> Any:
        """An action on a session: 409 while it is BUILDING or when it FAILED."""
        entry = self.entry(sid)
        with entry.lock:
            s = entry.session
            if s is None:
                if entry.status == "FAILED":
                    raise SimError(409, entry.detail or "The session could not be prepared.")
                raise SimError(409, BUILDING_TEXT)
            out = fn(s)
            entry.status = s.status
            entry.cursor = s.cursor
            return out

    def _setup_stored(self, body: dict[str, Any], row: dict[str, Any]) -> Setup:
        inst = self._instrument(body["instrument"])
        tracked = inst.tracked or inst.key
        spec = self.specs.get(tracked) or self.specs.get(inst.key)
        if spec is None:
            raise SimError(404, f"{inst.key} has no instrument in config/universe.yaml.")
        series = self.series.get(inst.key)
        params = {**DEFAULTS, **SESSION_DEFAULTS, **body["params"]}
        return Setup(
            str(row["id"]),
            inst.key,
            series.label if series is not None else inst.key,
            inst.kind,
            inst.key,
            spec,
            series,
            str(row["strategy"]),
            str(row["timeframe"]),
            str(row["mode"]),
            _ts(row["start_ts"]),
            params,
            row["created_at"],
        )

    # ── trades of every session ───────────────────────────────────────────
    def trades(self, session_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        meta = self.store.sessions()
        rows = self.store.trades(session_id=session_id, status="CLOSED", limit=limit)
        out = []
        for r in rows:
            m = meta.get(r["session_id"], {})
            out.append(
                {
                    **closed_row(r),
                    "session_id": r["session_id"],
                    "instrument": m.get("instrument_key"),
                    "strategy": m.get("strategy"),
                    "timeframe": m.get("timeframe"),
                    "mode": m.get("mode"),
                }
            )
        return out

    def trades_csv(self, session_id: str | None = None, limit: int = 100000) -> str:
        rows = self.trades(session_id, limit)
        cols = [
            "session_id",
            "instrument",
            "strategy",
            "timeframe",
            "mode",
            "id",
            "side",
            "action",
            "qty",
            "what",
            "source",
            "entry_ts",
            "entry",
            "entry_price",
            "stop0",
            "target0",
            "stop",
            "target",
            "stage",
            "best",
            "exit_ts",
            "exit",
            "exit_price",
            "exit_reason",
            "gross",
            "charges",
            "net",
            "r_multiple",
        ]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            flat = {**r, "what": (r.get("traded") or {}).get("what")}
            w.writerow(["" if flat.get(c) is None else flat.get(c) for c in cols])
        return buf.getvalue()
