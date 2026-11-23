"""The Simulator's sessions by id: created, kept in memory, rebuilt from `sim_sessions` at
their cursor when not in memory (a restart, a new desk). One manager per running desk
(`routes.register`), the way `api/server.py` keeps the Machine Learning Lab."""

from __future__ import annotations

import csv
import datetime as dt
import io
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine

from ..clock import IST, now_ist
from ..config import CostConfig
from ..costs import CostModel
from ..data.universe import load_specs
from ..desk.config import DeskInstrumentCfg, load_desk_config
from ..settings import current_settings
from ..strategies.registry import REGISTRY, SUITABLE
from .bars import BarSource, DeskBars, StoreBars
from .engine import (
    ATR_TIMEFRAMES,
    DEFAULTS,
    SESSION_DEFAULTS,
    TRAINING,
    Deps,
    Setup,
    SimError,
    SimSession,
    closed_row,
)
from .pricing import open_option_store
from .store import SimStore

#: the timeframes a single-timeframe strategy can be simulated on
TIMEFRAMES: tuple[str, ...] = ("1m", "3m", "5m", "10m", "15m", "30m", "60m")
SPEEDS: tuple[int, ...] = (1, 2, 5, 10, 60, 300)
OPTIONS_TTL_S = 300.0
#: what a strategy's `SUITABLE` categories mean for the desk's two kinds
_KIND_OF = {"OPTIONS": "INDEX", "STOCK": "STOCK"}


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


class SimManager:
    def __init__(
        self,
        engine: Engine,
        desk: Any = None,
        deps: Deps | None = None,
        bars: BarSource | None = None,
        clock: Callable[[], dt.datetime] = now_ist,
        instruments: tuple[DeskInstrumentCfg, ...] | None = None,
    ) -> None:
        self.engine = engine
        self.desk = desk
        self.clock = clock
        self.deps = deps or default_deps(desk, clock=clock)
        self.store = SimStore(engine)
        self.store_bars: BarSource = bars or StoreBars(engine)
        self.series, self.specs = load_specs()
        self.instruments = instruments or load_desk_config().instruments
        self.sessions: dict[str, SimSession] = {}
        self.lock = threading.RLock()
        self._options: tuple[float, dict[str, Any]] | None = None

    # ── what can be simulated ─────────────────────────────────────────────
    def _live_keys(self) -> set[str]:
        trackers = getattr(self.desk, "trackers", None) or {}
        return set(trackers)

    def _range(self, key: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        try:
            return self.store_bars.minute_range(key)
        except Exception:  # noqa: BLE001 - an instrument without bars lists no range
            return None

    def options(self) -> dict[str, Any]:
        with self.lock:
            hit = self._options
            if hit is not None and time.monotonic() - hit[0] < OPTIONS_TTL_S:
                return hit[1]
        live = self._live_keys()
        rows = []
        for inst in self.instruments:
            rng = self._range(inst.key)
            series = self.series.get(inst.key)
            rows.append(
                {
                    "key": inst.key,
                    "label": series.label if series is not None else inst.key,
                    "kind": inst.kind,
                    "first": rng[0].isoformat() if rng else None,
                    "last": rng[1].isoformat() if rng else None,
                    "live": inst.key in live,
                    "trade": inst.key not in TRAINING,
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
                }
            )
        out = {
            "instruments": rows,
            "strategies": strategies,
            "speeds": list(SPEEDS),
            "defaults": {
                "capital": SESSION_DEFAULTS["capital"],
                "stop_atr_k": DEFAULTS["stop_atr_k"],
                "target_r": DEFAULTS["target_r"],
                "extend": DEFAULTS["extend"],
                "stop_atr_tf": DEFAULTS["stop_atr_tf"],
            },
        }
        with self.lock:
            self._options = (time.monotonic(), out)
        return out

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
        try:
            for k in ("stop_atr_k", "target_r", "capital"):
                params[k] = float(params[k])
                if not params[k] > 0:
                    raise ValueError(k)
        except (TypeError, ValueError):
            raise SimError(
                400, "stop_atr_k, target_r and capital must be positive numbers."
            ) from None
        params["stop_atr_tf"] = str(params["stop_atr_tf"])
        if params["stop_atr_tf"] not in ATR_TIMEFRAMES:
            raise SimError(400, "stop_atr_tf must be 60m or own.")
        params["extend"] = bool(params["extend"])
        params["auto_exit"] = bool(params["auto_exit"])
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
            if start <= rng[0] or start > rng[1]:
                raise SimError(
                    400,
                    f"The start is outside the stored data of {inst.key}: "
                    f"{rng[0]:%Y-%m-%d %H:%M} to {rng[1]:%Y-%m-%d %H:%M}.",
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

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        sid = f"s_{secrets.token_hex(4)}"
        setup = self._setup(body, sid)
        session = SimSession(setup, self.deps, self._bars(setup.mode), self.store)
        self.store.add_session(
            {
                "id": sid,
                "created_at": setup.created_at,
                "instrument_key": setup.instrument_key,
                "strategy": setup.strategy,
                "timeframe": setup.timeframe,
                "mode": setup.mode,
                "start_ts": setup.start.to_pydatetime(),
                "cursor_ts": pd.Timestamp(session.cursor_ns, tz="UTC").to_pydatetime(),
                "status": session.status,
                "params": session._params_json(),
            }
        )
        with self.lock:
            self.sessions[sid] = session
        return {"session": session.session_view(), "candles": session.candles_view(),
                "state": session.state()}  # fmt: skip

    def get(self, sid: str) -> SimSession:
        with self.lock:
            hit = self.sessions.get(sid)
        if hit is not None:
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
        session = SimSession(setup, self.deps, self._bars(row["mode"]), self.store, restore=row)
        with self.lock:
            self.sessions.setdefault(sid, session)
            return self.sessions[sid]

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
