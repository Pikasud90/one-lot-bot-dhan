"""Offline harness shared by the US-002 acceptance tests (paper trades managed to a close).

Nothing here touches the network, a broker, `current_settings()` or the wall clock. The new
modules (`onelot.desk.rules`, `onelot.paper.manage`) are imported lazily, inside the helpers
that need them, so a missing module makes each test fail on its own (ImportError) instead of
breaking the collection of the whole file.

Conventions used by every test (they extend `us001_helpers`, which is imported, not copied):

* money is integer paise; bars are WRITTEN in paise as (open, high, low, close) tuples and
  become rupee floats only here (`p / 100` is the correctly rounded float of the price);
* the worked stock scenario: a long SBIN alert at 1000.00 with stop 990.00 and target 1020.00;
  the fill bar (10:06) opens at 1000.00, so the fill is 100030 paise (US-001, slippage 0.03 %),
  the initial risk is 1030 paise and the tick is 5 paise. Its short twin fills at 99970;
* the index scenario: BANKNIFTY at 54000 with stop 53850 and target 54300 (long, BUY_CE) or
  stop 54150 and target 53700 (short on the underlying, BUY_PE);
* `manage_step` is driven directly through `mk_state` / `mk_rules` / `run`; the book through
  `make_alert` (an `us001_helpers` alert whose `manage` block is pinned) and `step_book`.

Contract assumptions the tests share (each is listed in the TEST REPORT as an ambiguity):

* the CLOSED event's `detail` carries the `PaperClose` field names `reason`, `price_paise`
  and `gap`, and its `ts` is the close time (`close_of` reads them);
* the fine-bar resolution is inferred from the bar spacing (never from `ManageRules.tf_min`,
  which is the STRATEGY timeframe used by `bars_held`);
* for an INDEX position `PositionState.entry_paise` and every price of `manage_step` are on
  the UNDERLYING (the book prices the option);
* `last_bar_ts is None` means "no bar managed yet": the first bar managed is the one that
  opens at `opened_at` (the fill bar), inclusive.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib
import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from fractions import Fraction
from typing import Any, NamedTuple

import numpy as np
import pandas as pd
import us001_helpers as H

from onelot.config import BacktestCfg

# ── the worked scenarios, in paise ────────────────────────────────────────────────────
TICK = 5
ENTRY_L, STOP_L, TARGET_L = 100030, 99000, 102000  # long: fill 1000.30, stop 990, target 1020
ENTRY_S, STOP_S, TARGET_S = 99970, 101000, 98000  # short twin: fill 999.70
RISK = ENTRY_L - STOP_L  # 1030 paise (the short twin has the same risk)
U_ENTRY = 5_400_000  # BANKNIFTY 54000.00
U_STOP_L, U_TARGET_L = 5_385_000, 5_430_000  # BUY_CE (long on the underlying)
U_STOP_S, U_TARGET_S = 5_415_000, 5_370_000  # BUY_PE (short on the underlying)
PRE_BAR = (99800, 99900, 99700, 99800)  # the 10:05 bar: before the alert, never eligible
FILL_TS = H.at(10, 6)  # the fill bar of every worked scenario

BASE_MANAGE: dict[str, Any] = {
    "target_r": 2.0,
    "trail_atr_mult": 0.0,  # the trail is OFF unless a test turns it on
    "trail_activate_r": 1.0,
    "max_bars": 0,
    "exit_time": "15:15",
    "atr": 4.0,  # frozen at alert time; a 1.5 multiple is exactly 6.00 rupees = 600 paise
    "tf_min": 10,
    "horizon": "intraday",
}


# ── lazy access to the modules under test ─────────────────────────────────────────────
def manage() -> Any:
    return importlib.import_module("onelot.paper.manage")


def rules_mod() -> Any:
    return importlib.import_module("onelot.desk.rules")


# ── bars, written in paise ────────────────────────────────────────────────────────────
Row = tuple[int, int, int, int]  # open, high, low, close in paise


def pbars(start: dt.datetime, rows: Sequence[Row], step: int = 1) -> pd.DataFrame:
    """A tz-aware IST frame of `rows` (paise) every `step` minutes from `start`."""
    stamps = [start + dt.timedelta(minutes=step * i) for i in range(len(rows))]
    arr = np.asarray(rows, dtype=np.int64).reshape(len(rows), 4) / 100.0
    return pd.DataFrame(
        {
            "open": arr[:, 0],
            "high": arr[:, 1],
            "low": arr[:, 2],
            "close": arr[:, 3],
            "volume": np.full(len(rows), 1000.0),
        },
        index=pd.DatetimeIndex(stamps),
    )


def pbars_at(stamps: Sequence[dt.datetime], rows: Sequence[Row]) -> pd.DataFrame:
    """Like `pbars` but on explicit bar-open stamps (holes, two sessions, ...)."""
    arr = np.asarray(rows, dtype=np.int64).reshape(len(rows), 4) / 100.0
    return pd.DataFrame(
        {
            "open": arr[:, 0],
            "high": arr[:, 1],
            "low": arr[:, 2],
            "close": arr[:, 3],
            "volume": np.full(len(rows), 1000.0),
        },
        index=pd.DatetimeIndex(list(stamps)),
    )


def flat(price: int = 100000, n: int = 1) -> list[Row]:
    """`n` quiet bars that never touch a stop or target of the worked scenarios."""
    return [(price, price + 10, price - 10, price)] * n


def story(rows: Sequence[Row], step: int = 1) -> pd.DataFrame:
    """The 10:05 pre-alert bar followed by `rows` from the 10:06 fill bar on."""
    return pbars(H.at(10, 5), [PRE_BAR, *rows], step)


def resolution_of(bars: pd.DataFrame) -> int:
    """The most common bar spacing in whole minutes (1 for a single bar)."""
    if len(bars) < 2:
        return 1
    steps = pd.Series(np.diff(np.sort(bars.index.as_unit("ns").asi8)) // 60_000_000_000)
    return int(steps[steps > 0].mode().iloc[0])


def end_of(bars: pd.DataFrame) -> dt.datetime:
    """The close time of the last bar: the earliest `now` at which every bar is eligible."""
    return (bars.index.max() + pd.Timedelta(minutes=resolution_of(bars))).to_pydatetime()


def to_utc(bars: pd.DataFrame) -> pd.DataFrame:
    return bars.tz_convert("UTC")


# ── manage_step, driven directly ──────────────────────────────────────────────────────
def mk_state(
    direction: str = "long",
    kind: str = "STOCK",
    *,
    entry: int | None = None,
    stop: int | None = None,
    initial_stop: int | None = None,
    target: int | None | str = "default",
    opened_at: dt.datetime | None = None,
    last_bar_ts: dt.datetime | None = None,
    trail_active: bool = False,
    qty: int = 1,
    option: dict | None = None,
    bars_held: int = 0,
    sessions_held: int = 0,
) -> Any:
    """A PositionState. Defaults: the stock scenario of `direction` (index levels for INDEX)."""
    long_ = direction == "long"
    if kind == "INDEX":
        dflt_entry, dflt_stop = U_ENTRY, (U_STOP_L if long_ else U_STOP_S)
        dflt_target = U_TARGET_L if long_ else U_TARGET_S
    else:
        dflt_entry, dflt_stop = (ENTRY_L, STOP_L) if long_ else (ENTRY_S, STOP_S)
        dflt_target = TARGET_L if long_ else TARGET_S
    stop = dflt_stop if stop is None else stop
    contract: dict[str, Any] = {
        "direction": direction,
        "kind": kind,
        "qty": qty,
        "entry_paise": dflt_entry if entry is None else entry,
        "initial_stop_paise": stop if initial_stop is None else initial_stop,
        "stop_paise": stop,
        "target_paise": dflt_target if target == "default" else target,
        "trail_active": trail_active,
        "opened_at": opened_at or FILL_TS,
        "last_bar_ts": last_bar_ts,
        "bars_held": bars_held,
        "sessions_held": sessions_held,
        "option": option,
    }
    cls = manage().PositionState
    # A field the story's contract does not list (the pair may add `trade_id`, `closed`): fill
    # it only when it has no default, so the tests keep following the CONTRACT, not the code.
    for f in dataclasses.fields(cls):
        required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        if f.name not in contract and required:
            contract[f.name] = 7 if f.name == "trade_id" else None
    return cls(**contract)


def mk_rules(**kw: Any) -> Any:
    """ManageRules with the trail OFF, no time stop, exit 15:15 and a 5-paise tick."""
    base: dict[str, Any] = {
        "target_r": 2.0,
        "trail_atr_mult": 0.0,
        "trail_activate_r": 1.0,
        "atr": None,
        "max_bars": 0,
        "tf_min": 10,
        "exit_time": dt.time(15, 15),
        "tick_paise": TICK,
    }
    return manage().ManageRules(**{**base, **kw})


def trail_rules(mult: float = 1.5, activate: float = 1.0, atr: float | None = 4.0, **kw: Any):
    """Trail rules on 1-MINUTE strategy bars (tf_min 1) unless a test says otherwise: the trail
    arms and ratchets at strategy-bar closes only (Manager decision, cycle 2), and with tf_min 1
    every fine bar close is one. Tests of tf_min > 1 boundaries pass tf_min explicitly."""
    kw.setdefault("tf_min", 1)
    return mk_rules(trail_atr_mult=mult, trail_activate_r=activate, atr=atr, **kw)


def strategy_exit(reason: str, bar_close_ts: dt.datetime) -> Any:
    return manage().StrategyExit(reason=reason, bar_close_ts=bar_close_ts)


def run(
    state: Any,
    bars: pd.DataFrame,
    rules: Any | None = None,
    now: dt.datetime | None = None,
    se: Any | None = None,
) -> tuple[Any, list[Any]]:
    """manage_step(state, bars, rules, now, se); `now` defaults to the close of the last bar."""
    return manage().manage_step(
        state, bars, rules if rules is not None else mk_rules(), now or end_of(bars), se
    )


class Close(NamedTuple):
    reason: str
    price: int
    at: dt.datetime
    gap: bool
    event: Any


def closes_of(events: Iterable[Any]) -> list[Any]:
    return [e for e in events if str(H.val(e.kind)) == "CLOSED"]


def close_of(events: Iterable[Any]) -> Close | None:
    """The one CLOSED event as (reason, price_paise, at, gap), or None when nothing closed."""
    found = closes_of(events)
    assert len(found) <= 1, f"a trade closes at most once, got {len(found)} CLOSED events"
    if not found:
        return None
    event = found[0]
    detail = dict(event.detail)
    nested = detail.get("close")
    if isinstance(nested, dict):
        detail = nested
    for key in ("reason", "price_paise", "gap"):
        assert key in detail, f"the CLOSED detail must carry {key!r} (PaperClose): {detail}"
    return Close(
        str(H.val(detail["reason"])),
        int(detail["price_paise"]),
        event.ts,
        bool(detail["gap"]),
        event,
    )


def canon(events: Iterable[Any], *, with_trade: bool = False) -> list[tuple]:
    """Events as comparable tuples (kind, instant, detail JSON); trade ids only on request."""
    out = []
    for e in events:
        ts = e.ts.astimezone(dt.timezone.utc).isoformat()
        text = json.dumps(dict(e.detail), sort_keys=True, default=str)
        out.append(((e.trade_id,) if with_trade else ()) + (str(H.val(e.kind)), ts, text))
    return out


def upto(events: Iterable[Any], t: dt.datetime) -> list[Any]:
    """The events whose time is at or before `t`."""
    return [e for e in events if e.ts <= t]


def mentions(detail: Any, number: int) -> bool:
    """True when `number` appears as a whole integer anywhere in the JSON text of `detail`."""
    text = json.dumps(detail, default=str)
    return re.search(rf"(?<![\d.]){number}(?![\d.]|\d)", text) is not None


# ── the book, driven through its public contract ──────────────────────────────────────
def make_alert(alert_id: str = "A1", manage: dict | None = None, **kw: Any) -> dict[str, Any]:
    """An `us001_helpers` alert whose `manage` block is pinned (trail off unless overridden)."""
    horizon = kw.pop("horizon", "intraday")
    alert = H.make_alert(alert_id, **kw)
    alert["horizon"] = horizon
    merged = {**BASE_MANAGE, "horizon": horizon, **(manage or {})}
    if merged.get("trail_atr_mult") and "tf_min" not in (manage or {}):
        merged["tf_min"] = 1  # a trail test runs on 1-minute strategy bars (see trail_rules)
    alert["manage"] = merged
    return alert


def step_book(
    book: Any,
    frame: pd.DataFrame,
    now: dt.datetime | None = None,
    se: Callable[[Any, dt.datetime], Any] | None = None,
    series: str = "SBIN",
) -> list[Any]:
    """book.step(now, feed, se) over one series; `now` defaults to the close of the last bar."""
    feed = H.Feed(**{series: frame})
    when = now or end_of(frame)
    return book.step(when, feed) if se is None else book.step(when, feed, se)


def opened(book: Any, alert: dict | None = None, **kw: Any) -> dict[str, Any]:
    alert = alert if alert is not None else make_alert(**kw)
    H.open_trade(book, alert)
    return alert


def row_of(engine: Any, alert_id: str = "A1") -> dict[str, Any]:
    row = dict(H.trade_row(engine, alert_id))
    row["status"] = H.val(row["status"])
    return row


def kinds(events: Iterable[Any]) -> list[str]:
    return [str(H.val(e.kind)) for e in events]


def stored_close_events(engine: Any, alert_id: str = "A1") -> list[dict]:
    return [e for e in H.event_rows(engine, alert_id) if str(H.val(e["kind"])) == "CLOSED"]


# ── the stock exit oracle (Manager decision 2, 2026-11-01: stock exits mirror the Lab) ─
def expected_stock_exit(
    level_paise: int, direction: str, reason: str, slippage_pct: float = 0.03, tick: int = TICK
) -> int:
    """Exact: the exit a STOCK trade books for a rule `level_paise`.

    A target is a limit fill and is NOT slipped. Every other exit (stop, trailed stop, gap at the
    open, strategy exit, time stop, session end) is slipped by `slippage_pct` (a PERCENT)
    against the trader, then snapped to the tick against the trader: a long SELLS (down), a
    short BUYS BACK (up). All in rational arithmetic; the mirror of `H.expected_stock_entry`.
    """
    if reason == "TARGET":
        return level_paise
    f = Fraction(str(slippage_pct)) / 100
    if direction == "long":
        return tick * math.floor(Fraction(level_paise) * (1 - f) / tick)
    return tick * math.ceil(Fraction(level_paise) * (1 + f) / tick)


def expected_gross(entry: int, exit_: int, direction: str, qty: int = 1) -> int:
    """sign x (exit - entry) x qty, in paise (a stock; an option is long premium: sign +1)."""
    return (1 if direction == "long" else -1) * (exit_ - entry) * qty


# ── many trades on many series (AC8) ──────────────────────────────────────────────────
def many_specs(n: int) -> dict[str, Any]:
    """The harness specs plus `n` extra one-share stocks S0..S{n-1}."""
    from onelot.data.universe import InstrumentSpec

    specs = dict(H.SPECS)
    for i in range(n):
        key = f"S{i}"
        specs[key] = InstrumentSpec(
            key, f"{key} (1 share)", "STOCK", key, 1, 0.05, "equity_intraday", 0.0
        )
    return specs


def history_stamps(sessions: int = 80, last_day: dt.date = H.DAY) -> list[pd.Timestamp]:
    """`sessions` full NSE sessions of 1-minute stamps (375 each) ending on `last_day`."""
    days = pd.bdate_range(end=pd.Timestamp(last_day), periods=sessions)
    out: list[pd.Timestamp] = []
    for day in days:
        out += list(
            pd.date_range(
                day + pd.Timedelta(hours=9, minutes=15), periods=375, freq="min", tz="Asia/Kolkata"
            )
        )
    return out


# ── option oracle: lab.execution.execute_trade for the EXIT leg and the gross ─────────
def oracle_option_roundtrip(
    *,
    bt: BacktestCfg,
    spec: Any,
    opt_type: str,
    underlying_entry: float,
    underlying_exit: float,
    entry_ts: dt.datetime,
    exit_ts: dt.datetime,
    pricer: Callable[[Any, float, Any], float | None],
    stop: float | None = None,
) -> tuple[int, int, int]:
    """(entry_paise, exit_paise, gross_paise) that `lab.execution.execute_trade` books for a
    long option bought at `underlying_entry` and sold at `underlying_exit` (one full leg).

    The Lab prices the exit leg at the underlying LEVEL of the leg (a stop, a target, an open or
    a close) with the same frozen option, so this is the differential oracle for the option
    exit fill and for gross P&L (costs are US-023's and are not part of this oracle).
    """
    from onelot.costs import CostModel
    from onelot.lab.execution import execute_trade
    from onelot.lab.simulator import Leg, SimTrade

    class Pricer:
        def iv_for(self, ts):
            return 0.16

        def premium(self, spot, k, ts, expiry, opt):
            return pricer({"opt_type": opt}, spot, ts)

        def delta(self, spot, k, ts, expiry, opt):
            return 0.65

    sign = 1 if opt_type == "CE" else -1
    stop = float(underlying_entry) - 150.0 * sign if stop is None else float(stop)
    t = SimTrade(
        symbol="BANKNIFTY",
        strategy="ema_cross",
        side=sign,
        signal_ts=pd.Timestamp(entry_ts) - pd.Timedelta(minutes=1),
        entry_ts=pd.Timestamp(entry_ts),
        entry_price=float(underlying_entry),
        stop_initial=stop,
        risk_ps=abs(float(underlying_entry) - stop),
        target=None,
        strength=60.0,
        atr=100.0,
        legs=[Leg(pd.Timestamp(exit_ts), float(underlying_exit), 1.0, "test")],
    )
    row = execute_trade(
        t, spec, CostModel(H._cost_cfg().product(spec.product)), bt, bt.slippage_pct, Pricer()
    )
    entry = H.to_paise(row["detail"]["premium_entry"])
    exit_ = H.to_paise(row["detail"]["premium_exit"])
    gross = H.to_paise(row["gross_pnl"])
    assert gross == (exit_ - entry) * spec.lot_size, "oracle: gross is long premium, exactly"
    return entry, exit_, gross


# ── the Desk harness for the characterisation tests ───────────────────────────────────
def desk_position(
    *,
    direction: str = "long",
    kind: str = "STOCK",
    series_key: str = "SBIN",
    entry: float = 1000.0,
    stop: float | None = None,
    target: float | None | str = "default",
    manage: dict | None = None,
    horizon: str = "intraday",
    opened_at: dt.datetime | None = None,
    option: dict | None = None,
    pid: str = "pos0001",
) -> dict[str, Any]:
    """A manual position dict exactly as `Desk.acknowledge` builds it (the shape the desk's
    `_manage_positions` reads and writes)."""
    long_ = direction == "long"
    stop = (entry - 10.0 if long_ else entry + 10.0) if stop is None else stop
    if target == "default":
        target = entry + 20.0 if long_ else entry - 20.0
    opened_at = opened_at or H.at(10, 0)
    side = ("BUY_CE" if long_ else "BUY_PE") if kind == "INDEX" else ("BUY" if long_ else "SELL")
    return {
        "id": pid,
        "signal_id": "sig0001",
        "instrument_key": "BANKNIFTY_OPT" if kind == "INDEX" else "SBIN",
        "series_key": series_key,
        "label": series_key,
        "kind": kind,
        "side": side,
        "direction": direction,
        "strategy": "ema_cross",
        "timeframe": "10m",
        "opened_at": opened_at.isoformat(),
        "qty": 30 if kind == "INDEX" else 1,
        "entry_price": entry,
        "entry_underlying": entry,
        "stop_underlying": stop,
        "target_underlying": target,
        "initial_stop_underlying": stop,
        "option": option or {},
        "manage": {**BASE_MANAGE, "horizon": horizon, **(manage or {})},
        "horizon": horizon,
        "trail_active": False,
        "bars_held": 0,
        "sessions_held": 0,
        "last_underlying": entry,
        "unrealized": 0.0,
        "r_now": 0.0,
        "exit_advice": None,
        "hold_check": "monitoring",
        "status": "OPEN",
        "source": "dashboard",
        "notes": [],
        "confidence": None,
    }


def close_series(
    closes: Sequence[float], start: dt.datetime, step: dt.timedelta = dt.timedelta(minutes=2)
) -> pd.DataFrame:
    """A frame whose LAST close is the price the desk reads as the live price."""
    stamps = [start + step * i for i in range(len(closes))]
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": np.full(len(c), 1000.0)},
        index=pd.DatetimeIndex(stamps),
    )


def forbid_governor_mutation(monkeypatch: Any) -> None:
    """Any call that changes the RiskGovernor now fails the test that makes it."""
    from onelot.risk.governor import RiskGovernor

    def refuse(name: str) -> Callable[..., Any]:
        def boom(self: Any, *_a: Any, **_k: Any) -> None:
            raise AssertionError(f"the paper book must not call RiskGovernor.{name}")

        return boom

    for name in (
        "record_open",
        "record_close",
        "halt",
        "manual_panic_halt",
        "reset_halt",
        "sync_open_positions",
    ):
        monkeypatch.setattr(RiskGovernor, name, refuse(name))
