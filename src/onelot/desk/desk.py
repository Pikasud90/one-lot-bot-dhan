"""The desk orchestrator.

One thread: pull bars for every watchlist series (Dhan 1-minute bars + the engine's
WebSocket ticks when the Data API is active, else one batched Yahoo call) -> update
trackers -> evaluate every strategy slot on its own closed bars -> raise BUY/SELL
alerts (one-unit economics, confidence, alert text, time-to-live) -> manage the
positions the operator has confirmed (trail, target, stop, strategy exit, square-off
for intraday, session time stop for swing) -> re-audit every archived alert against
the bars that followed -> cache a versioned snapshot the SSE stream ships.

Alert lifecycle (owner feedback OF-5, 2026-11-07): every alert opens a paper trade, the
paper trade's close IS the alert's one exit (one EXIT notification, never repeated), and a
position the owner took closes at that exit alert's price. Prices are the alerts'; the
owner may report their own entry and exit to see the difference. The risk governor gates
neither alerts nor paper trades, and the desk's trades no longer feed it (it still guards the
Bank Nifty engine); one live idea per instrument keeps alerts from repeating.
Nothing here can place an order.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import math
import threading
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlalchemy import Engine, select, update

from ..broker.base import Contract
from ..clock import IST, now_ist, to_ist
from ..data.schema import desk_positions, desk_signals
from ..data.universe import load_specs
from ..indicators import mtf
from ..lab.real_options import premium_in_minute
from ..notify.base import Notification, NotificationLevel, shown_confidence
from .audit import audit_signal, confidence, strategy_scoreboard
from .config import DeskConfig, load_desk_config
from .data import CompositeBars, build_source, is_delayed_source
from .economics import (
    Economics,
    alert_text,
    index_option_economics,
    index_option_economics_from_chain,
    stock_economics,
)
from .fillcost import FillCostRecorder, Watched, charges_check
from .history import daily_history, minute_history
from .rules import pick_expiry
from .tracker import InstrumentTracker, StrategySlot, Verdict

if TYPE_CHECKING:  # annotation only: the paper package is never imported by the desk
    from ..paper.book import PaperBook

log = logging.getLogger(__name__)

# Leads the title of a SIGNAL notification built on delayed (Yahoo) bars, so a truncated
# OS toast still shows it. The alert payload keeps the flag in data.delayed / delay_note.
DELAYED_TITLE_MARKER = "(delayed ~15m)"
# The card of an intraday position left open from an earlier session (US-004 decision E).
SESSION_OVER_NOTE = "session over: record the broker square-off"
# A paper trade in one of these states is over: its close is the alert's exit (OF-5).
FINAL_PAPER = ("CLOSED", "NO_FILL")
# _paper_link's answer when the book could not be read this poll: not the same as "no paper
# trade", so the desk's own advice never becomes a second exit (QA cycle 1, BUG-3).
UNREADABLE = object()
# A price the owner types is a price per share or per option unit: anything at or above this
# is a typo (1e308 made the journal unserialisable, QA cycle 1 BUG-11).
MAX_REPORTED_PRICE = 1e7
# The stored daily bars of the trend ladder are read once a calendar day. A read that failed
# is tried again after the first delay, a store with no history for the series after the second.
HISTORY_RETRY = dt.timedelta(minutes=10)
HISTORY_EMPTY_RETRY = dt.timedelta(hours=1)


def _reported(value: object) -> float | None:
    """A price the owner typed: finite, above zero and below MAX_REPORTED_PRICE, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and 0 < price < MAX_REPORTED_PRICE else None


def _slot_of(tracker: InstrumentTracker, strategy: str, timeframe: str) -> StrategySlot | None:
    """The tracker's slot that runs `strategy` on `timeframe` (an alert names both), or None."""
    return next(
        (s for s in tracker.slots if s.cfg.name == strategy and s.cfg.timeframe == timeframe),
        None,
    )


def _rupees(paise: object) -> float | None:
    """Integer paise from the paper book as rupees for the desk's float bookkeeping."""
    if paise is None or isinstance(paise, bool) or not isinstance(paise, int):
        return None
    return paise / 100.0


# The prices a stored position may be patched with: the owner's and the alert's exit.
PRICE_PATCH = ("user_entry_price", "user_exit_price", "alert_exit_price")
PRICE_FIELDS = (
    "alert_entry_price",
    "alert_exit_price",
    "user_entry_price",
    "user_exit_price",
    "alert_net_pnl",
    "user_net_pnl",
    "diff_pnl",
    "net_pnl",
)


def _prices(p: dict) -> dict:
    """The alert's and the owner's prices of a taken position, and their P&L (OF-5)."""
    out = {k: p.get(k) for k in PRICE_FIELDS}
    if out["alert_entry_price"] is None:
        out["alert_entry_price"] = p.get("entry_price")
    return out


def _exit_info(trade: Any) -> dict:
    """An alert's exit (OF-5) from its final paper trade: what the archive and the EXIT show."""
    when = trade.exit_ts or trade.updated_at
    return {
        "trade_id": trade.id,
        "status": trade.status,
        "reason": str(trade.close_reason or trade.status),
        "at": _iso(_aware(when)),
        "price": _rupees(trade.exit_paise),
        "net": _rupees(trade.net_paise),
        "grade": trade.grade,
    }


def _exit_reason_line(info: dict, words: str, at_price: str) -> str:
    """The reason the phone prints for an alert's exit; a no-fill says NO FILL."""
    if info["status"] != "NO_FILL":
        return f"{words}{at_price}"
    return "NO FILL" if words == "NO FILL" else f"NO FILL ({words})"


def _trade_brief(p: dict) -> dict:
    """What the alert archive shows of the position taken on it."""
    return {
        "net_pnl": p.get("net_pnl"),
        "exit_price": p.get("exit_price"),
        "reason": p.get("exit_reason"),
        "source": p.get("exit_source"),
        "user_net_pnl": p.get("user_net_pnl"),
        "diff_pnl": p.get("diff_pnl"),
    }


def _iso(v: dt.datetime | None) -> str | None:
    return v.isoformat() if v else None


def _conf_number(conf: dict) -> str:
    """The title's confidence number: the same floored rule as the phone (US-003 BUG-4)."""
    shown = shown_confidence(conf.get("score"))
    return "n/a" if shown is None else str(shown)


def _aware(v: dt.datetime | None) -> dt.datetime | None:
    if v is None:
        return None
    return (
        v.replace(tzinfo=dt.timezone.utc).astimezone(IST) if v.tzinfo is None else v.astimezone(IST)
    )


class Desk:
    def __init__(
        self,
        engine: Engine,
        cfg_app,
        notifier,
        governor,
        session_clock,
        repo=None,
        adapter_getter=None,
        data_api_active=None,
        desk_cfg: DeskConfig | None = None,
        feed_getter=None,
        calendar=None,
        paper: PaperBook | None = None,
    ) -> None:
        self.engine = engine
        # The paper book (US-001): opens a paper trade per alert and fills it on later polls.
        # Evidence, not permission: nothing it does (or fails to do) can stop an alert, its
        # notification or the poll. Its failures are counted here (Manager decision 5).
        self.paper = paper
        self.hook_errors = 0
        self.app_cfg = cfg_app
        self.notifier = notifier
        self.governor = governor
        self.session_clock = session_clock
        self.repo = repo
        self.calendar = calendar
        self._adapter_getter = adapter_getter or (lambda: None)
        self._data_api_active = data_api_active or (lambda: False)
        self._feed_getter = feed_getter or (lambda: None)
        self.cfg = desk_cfg or load_desk_config()
        self.series, self.instruments = load_specs()
        self.trackers: dict[str, InstrumentTracker] = {}
        self._build_trackers()
        self.source: CompositeBars = build_source(
            self.cfg.source,
            self._adapter_getter(),
            bool(self._data_api_active()),
            self._feed_getter(),
            self._all_specs(),
        )
        self.fine_minutes = int(self.source.resolution[:-1])
        for t in self.trackers.values():
            t.fine_minutes = self.fine_minutes
        self.context_bars: dict[str, pd.DataFrame] = {}
        self.context_manual: dict[str, dict] = {}
        self.signals: dict[str, dict] = {}  # pending, by id
        self.positions: dict[str, dict] = {}  # open, by id
        self.archive: list[dict] = []  # recent signals (any status) with outcomes, newest first
        self.scoreboard: dict[str, dict] = {}
        self._chain_cache: dict[str, tuple[dt.datetime, Any]] = {}
        # the alerted contracts' one-minute bars for the paper book's real premiums
        self._contract_cache: dict[str, tuple[dt.datetime, pd.DataFrame]] = {}
        self._board_cache: tuple[dt.datetime | None, dict] = (None, {})
        self.version = 0
        self._snapshot: dict = {
            "updated_at": None,
            "indices": [],
            "stocks": [],
            "context": [],
            "positions": [],
            "signals": [],
            "source": {"name": self.source.name},
            "booting": True,
            "version": 0,
            "paper": None,
        }
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_fetch_at: dt.datetime | None = None
        self.last_error: str | None = None
        self.polls = 0
        # stored days for the trend ladder: when a series is next tried after a read that
        # gave nothing, and the series whose failure is already in the log
        self._history_retry: dict[str, dt.datetime] = {}
        self._history_failed: set[str] = set()
        # when this process started: a delivery fill at an open the desk did not see is late,
        # or missed (US-067 D3)
        self.started_at = now_ist()
        self.delivery = self._build_delivery()
        self._recover()
        self._restore_slot_counters(now_ist())
        self._tell_paper_tracked()
        self._tell_paper_trail()
        from .pulse import MarketPulse

        self.pulse = MarketPulse(engine, self, self._adapter_getter, self._data_api_active)
        # what a one-unit fill costs: read-only quotes on its own thread (owner, 2026-11-07)
        self.fillcost = FillCostRecorder(
            engine,
            self._adapter_getter,
            lambda: bool(self._data_api_active()),
            self._market_open,
            self._fill_stocks,
            self._fill_indices,
            roll_days=self.cfg.option_roll_days,
        )

    def fill_charges(self, days: int = 365) -> dict:
        """Dhan's billed charges on the owner's own fills (trade history, a free read) beside
        the cost table's, cached for an hour. {} without a broker."""
        now = now_ist()
        hit = getattr(self, "_charges_cache", None)
        if hit is not None and (now - hit[0]).total_seconds() < 3600 and hit[1] == days:
            return hit[2]
        adapter = self._adapter_getter()
        if adapter is None:
            return {}
        from ..config import CostConfig
        from ..costs import CostModel

        table = CostConfig.load(self.app_cfg.project_root / "config" / "costs.yaml")
        trades = adapter.trade_history((now - dt.timedelta(days=days)).date(), now.date(), 60)
        out = {
            "days": days,
            "fills": len(trades),
            "by_product": charges_check(trades, lambda name: CostModel(table.product(name))),
        }
        self._charges_cache = (now, days, out)
        return out

    # ── fill-cost sampling inputs ─────────────────────────────────────────
    def _market_open(self, now: dt.datetime) -> bool:
        window = self.session_clock.current_window(now)
        return bool(window and window.is_open(now))

    def _fill_stocks(self) -> list[Watched]:
        return [
            Watched(t.instrument.key, str(t.series.dhan_security_id), t.series.dhan_segment,
                    t.series.key, "EQUITY")
            for t in list(self.trackers.values())
            if t.cfg.kind == "STOCK" and t.series.dhan_security_id
        ]  # fmt: skip

    def _fill_indices(self) -> list[tuple[str, str, str, str]]:
        return [
            (t.series.key, str(t.series.dhan_security_id), t.series.exchange, t.instrument.key)
            for t in list(self.trackers.values())
            if t.cfg.kind == "INDEX" and t.series.dhan_security_id
        ]

    # ── setup ─────────────────────────────────────────────────────────────
    def _all_specs(self) -> dict:
        specs = {k: t.series for k, t in self.trackers.items()}
        for c in self.cfg.context:
            if not c.manual and c.key in self.series and c.key not in specs:
                specs[c.key] = self.series[c.key]
        return specs

    def _build_trackers(self) -> None:
        self.trackers = {}
        for ic in self.cfg.instruments:
            s = self.series.get(ic.key)
            inst = self.instruments.get(ic.tracked)
            if s is None or inst is None:
                log.warning("desk: %s / %s not in universe.yaml; skipped", ic.key, ic.tracked)
                continue
            self.trackers[ic.key] = InstrumentTracker(ic, s, inst, getattr(self, "fine_minutes", 2))

    def reload(self) -> dict:
        with self._lock:
            self.cfg = load_desk_config()
            old = dict(self.trackers)
            self._build_trackers()
            for k, t in self.trackers.items():
                t.fine_minutes = self.fine_minutes
                if k in old:
                    t.bars = old[k].bars
                    # the stored days stay until the next poll reads them again (history_day
                    # is left unset, so a reload also picks up a backfill run meanwhile)
                    t.daily_history, t.history_note = old[k].daily_history, old[k].history_note
                    t.minute_history = old[k].minute_history
            self._history_retry.clear()
            self._tell_paper_tracked()
            self._wake.set()
        return {"instruments": len(self.trackers), "path": self.cfg.path}

    def _recover(self) -> None:
        now = now_ist()
        since = now - dt.timedelta(days=self.cfg.audit_days)
        with self.engine.connect() as conn:
            rows = (
                conn.execute(select(desk_positions).where(desk_positions.c.status == "OPEN"))
                .mappings()
                .all()
            )
            sigs = (
                conn.execute(
                    select(desk_signals)
                    .where(desk_signals.c.created_at >= since)
                    .order_by(desk_signals.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        for r in rows:
            p = dict(r["payload"] or {})
            p.update({"id": r["id"], "opened_at": _iso(_aware(r["opened_at"]))})
            self.positions[r["id"]] = p
        for r in sigs:
            p = dict(r["payload"] or {})
            p.update(
                {
                    "id": r["id"],
                    "status": r["status"],
                    "resolved_at": _iso(_aware(r["resolved_at"])),
                }
            )
            exp = _aware(r["expires_at"])
            if r["status"] == "PENDING":
                if exp and exp < now:
                    p["status"] = "EXPIRED"
                    self._resolve_signal_db(r["id"], "EXPIRED")
                else:
                    self.signals[r["id"]] = p
            self.archive.append(p)
        if self.positions:
            log.info("desk: recovered %d open position(s)", len(self.positions))
        self._recover_exits()

    def _restore_slot_counters(self, now: dt.datetime) -> None:
        """After a restart, each strategy slot's own limits continue where they were: today's
        alerts count toward its `max_trades_per_day`, and its latest archived exit restarts its
        cooldown (both lived in memory only)."""
        today = now.astimezone(IST).date()
        for sig in self.archive:
            t = self.trackers.get(str(sig.get("series_key") or ""))
            if t is None:
                continue
            slot = next(
                (
                    s
                    for s in t.slots
                    if s.cfg.name == sig.get("strategy") and s.cfg.timeframe == sig.get("timeframe")
                ),
                None,
            )
            if slot is None:
                continue
            created = sig.get("created_at")
            try:
                day = dt.datetime.fromisoformat(str(created)).astimezone(IST).date()
            except (TypeError, ValueError):
                day = None
            if day == today:
                slot.signals_today += 1
            exit_info = sig.get("exit") or {}
            if exit_info.get("at"):
                self._note_slot_exit(sig, exit_info)
        self._counter_day = today.isoformat()  # the first poll must not wipe what was restored

    def _recover_exits(self) -> None:
        """Archived alerts whose paper trade is over show that exit (OF-5), also the ones that
        exited before the exit was recorded on the alert. Memory only: nothing is written and
        nobody is told again. A book that cannot be read leaves the rows as they are."""
        if self.paper is None:
            return
        for sig in self.archive:
            if sig.get("exit") or sig.get("status") == "PENDING":
                continue
            try:
                trade = self.paper.for_alert(sig["id"])
            except Exception:  # noqa: BLE001 - the desk starts whatever the book does
                self.hook_errors += 1
                log.exception("paper: the exits of archived alerts could not be read")
                return
            if trade is not None and trade.status in FINAL_PAPER:
                sig["exit"] = _exit_info(trade)

    # ── thread ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="desk-loop", daemon=True)
        self._thread.start()
        self.fillcost.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.fillcost.stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception("desk poll failed")
            now = now_ist()
            window = self.session_clock.current_window(now)
            near = window is not None and (window.open_at - dt.timedelta(minutes=20)) <= now <= (
                window.close_at + dt.timedelta(minutes=5)
            )
            wait = self.cfg.poll_seconds if near else 300
            self._wake.wait(wait)
            self._wake.clear()

    # ── one poll ──────────────────────────────────────────────────────────
    def poll(self) -> None:
        now = now_ist()
        self._maybe_switch_source()
        self._load_daily_history(now)
        self._load_minute_history(now)
        self._delivery_history(now)
        specs = self._all_specs()
        since = {k: t.bars.index[-1] for k, t in self.trackers.items() if not t.bars.empty}
        got = self.source.fetch(specs, self.cfg.history_days, since)
        self.last_fetch_at = now
        self.last_error = self.source.last_error
        self.polls += 1
        quotes = getattr(self.source.primary, "quotes", None) or {}
        with self._lock:
            for k, df in got.items():
                if k in self.trackers:
                    self.trackers[k].update_bars(df)
                else:
                    self.context_bars[k] = df
                if k in self.trackers and k in (c.key for c in self.cfg.context):
                    self.context_bars[k] = self.trackers[k].bars
            for k, q in quotes.items():
                if k in self.trackers:
                    self.trackers[k].live_quote = q
            self._reset_daily_counters(now)
            window = self.session_clock.current_window(now)
            entries_ok = bool(window and window.entries_allowed(now))
            market_ok = bool(window and window.is_open(now))
            nifty = self.trackers.get("NIFTY")
            market_bars = nifty.bars if nifty is not None else self.context_bars.get("NIFTY")
            self._expire_signals(now)
            keep = self._active_slots()
            for t in self.trackers.values():
                fired = t.evaluate(
                    now, market_bars, entries_ok and market_ok, keep.get(t.series.key, ())
                )
                for slot, v in fired:
                    self._raise_signal(t, slot, v, now)
            if market_ok or self.positions:
                self._manage_positions(now, window)
            # Every poll, even with the market shut: a waiting paper trade must still be
            # able to close NO_FILL once its data reaches the session end.
            self._step_paper(now)
            self._step_delivery(now)
            self._audit_archive(now)
            self._rebuild_snapshot(now, window)
        try:
            self.pulse.maybe_record(now, market_ok)
        except Exception:  # noqa: BLE001
            log.exception("pulse failed")

    # ── the delivery pullback trades (US-067) ──────────────────────────────
    def _build_delivery(self) -> Any:
        """The delivery desk of the configured delivery slots, or None without one. A delivery
        desk that cannot be built is logged and the rest of the desk runs without it."""
        if not self.cfg.delivery_slots():
            return None
        try:
            from ..clock import TradingCalendar
            from ..config import CostConfig
            from ..delivery.runner import DeliveryDesk

            root = self.app_cfg.project_root
            calendar = self.calendar or TradingCalendar.from_yaml(
                root / "config" / "nse_holidays.yaml"
            )
            names = {
                k: (t.series.label, str(t.series.extra.get("trading_symbol") or t.series.key))
                for k, t in self.trackers.items()
            }
            delivery = DeliveryDesk(
                self.engine,
                self.cfg,
                CostConfig.load(root / "config" / "costs.yaml"),
                self.app_cfg.session,
                calendar,
                self.notifier,
                self.started_at,
                names=names,
                top_up=self._top_up_bars,
                data_api_active=lambda: bool(self._data_api_active()),
            )
            delivery.also_top_up = list(self.trackers)  # all 23: the indices' 4h rungs too
            return delivery
        except Exception:  # noqa: BLE001 - the intraday desk runs whatever this does
            log.exception("delivery: the delivery desk could not be built; it is off")
            return None

    def _top_up_bars(self, keys: list[str], stop: Callable[[], bool] | None = None) -> Any:
        """The delivery top-up (US-067 D8): the incremental one-minute backfill from Dhan for
        `keys`, stopped when `stop()` turns true (09:00 IST). Never an order: a read of the
        broker's history, written to the store. Returns the backfill's result (its errors)."""
        from ..data import backfill as bf

        adapter = self._adapter_getter()
        if adapter is None:
            raise RuntimeError("no Dhan adapter")
        return bf.backfill(
            self.engine, bf.choose_provider("dhan", adapter), keys, ["1m"], stop_flag=stop
        )

    def _delivery_history(self, now: dt.datetime) -> None:
        """Outside the desk lock: the delivery rule's stored days and the top-up."""
        if self.delivery is None:
            return
        try:
            self.delivery.load_history(now)
            self.delivery.maybe_top_up(now)
        except Exception:  # noqa: BLE001 - never stops the poll
            self.hook_errors += 1
            log.exception("delivery: loading the daily history failed")

    def _step_delivery(self, now: dt.datetime) -> None:
        if self.delivery is None:
            return

        def bars(key: str) -> pd.DataFrame | None:
            t = self.trackers.get(key)
            if t is not None:
                return t.bars
            return self.context_bars.get(key)

        def last(key: str) -> float | None:
            t = self.trackers.get(key)
            return t.last_price() if t is not None else None

        try:
            self.delivery.on_poll(now, bars, last)
        except Exception:  # noqa: BLE001 - the poll carries on whatever this does
            self.hook_errors += 1
            log.exception("delivery: the poll step failed")

    def _add_reversal(self, d: dict) -> None:
        """For an index whose micro-trend paper trade is open: how many of the 7 trends point
        against it, and at how many it exits (US-068). A copy of the card's trend is changed,
        never the tracker's own."""
        t = self.trackers.get(str(d.get("key")))
        trend = d.get("trend") or {}
        rows = trend.get("rows") or []
        if t is None or not rows or self.paper is None:
            return
        slot = next((s for s in t.slots if getattr(s.strategy, "ladder_timeframes", None)), None)
        if slot is None:
            return
        try:
            trades = [
                x
                for x in self.paper.open_trades()
                if x.series_key == t.series.key and x.strategy == slot.cfg.name
            ]
        except Exception:  # noqa: BLE001 - a reading for the card, never a dependency
            return
        if not trades or trades[0].status != "OPEN":
            return
        sign = 1 if trades[0].direction == "long" else -1
        k = sum(1 for r in rows if r.get("direction") == -sign)
        exit_at = int(getattr(getattr(slot.strategy, "p", None), "exit_count", 3))
        d["trend"] = {**trend, "reversal": {"reversed": k, "of": len(rows), "exit_at": exit_at}}

    def _active_slots(self) -> dict[str, set[tuple[str, str]]]:
        """series key -> (strategy, timeframe) of every trade still active on it: a paper trade
        waiting or open, or a taken position. A paused slot stays evaluated for these, so its
        trade is managed and closed as before (US-067 D16); no paused slot raises an alert."""
        out: dict[str, set[tuple[str, str]]] = {}
        open_trades = getattr(self.paper, "open_trades", None)
        if callable(open_trades):
            try:
                for tr in open_trades():
                    out.setdefault(tr.series_key, set()).add((tr.strategy, tr.timeframe))
            except Exception:  # noqa: BLE001 - the book never stops the poll
                self.hook_errors += 1
                log.exception("paper: open_trades() failed")
        for p in self.positions.values():
            key = str(p.get("series_key") or "")
            out.setdefault(key, set()).add((str(p.get("strategy")), str(p.get("timeframe"))))
        return out

    def _load_daily_history(self, now: dt.datetime) -> None:
        """Give every tracker the stored daily bars the 1d rung of its trend ladder needs
        (`desk/history.py`): once a calendar day, read-only, outside the desk lock.

        The polled bars cover about 25 days; a daily Supertrend(14) wants 70. A read that
        fails, or a store without history for the series, is tried again later and never
        stops the poll: the ladder then shows the 1d rung without a reading and says why."""
        today = now.date()
        due = [
            (key, t.series.key)
            for key, t in list(self.trackers.items())
            if t.history_day != today and self._history_retry.get(key, now) <= now
        ]
        for key, series_key in due:
            daily: pd.DataFrame | None = None
            try:
                daily = daily_history(self.engine, series_key, now)
            except Exception:  # noqa: BLE001 - a reading for the card, never a dependency
                if key not in self._history_failed:
                    log.exception("desk: the stored daily history of %s could not be read", key)
                self._history_failed.add(key)
            with self._lock:
                t = self.trackers.get(key)
                if t is None:
                    continue
                if daily is None:
                    self._history_retry[key] = now + HISTORY_RETRY
                    if t.daily_history is None:
                        t.history_note = (
                            "the stored daily history could not be read: no 1d trend reading"
                        )
                    continue
                self._history_failed.discard(key)
                t.daily_history = daily
                if daily.empty:
                    self._history_retry[key] = now + HISTORY_EMPTY_RETRY
                    t.history_note = (
                        "the store has no daily history for it: no 1d trend reading "
                        "(run a backfill)"
                    )
                else:
                    self._history_retry.pop(key, None)
                    t.history_day, t.history_note = today, None

    def _load_minute_history(self, now: dt.datetime) -> None:
        """The stored one-minute bars of every micro-trend ladder (US-068: its 4h rung needs
        about 35 sessions), once a calendar day; between 09:00 and 15:35 IST in a background
        thread whose result is swapped in, never on the poll thread."""
        today = now.astimezone(IST).date()
        if getattr(self, "_minute_day", None) == today:
            return
        loader = getattr(self, "_minute_loader", None)
        if loader is not None and loader.is_alive():
            return
        keys = [
            (k, t.series.key)
            for k, t in list(self.trackers.items())
            if t.ladder_timeframes != mtf.LADDER_TIMEFRAMES
        ]
        self._minute_day = today
        if not keys:
            return

        def load() -> None:
            for key, series_key in keys:
                try:
                    bars = minute_history(self.engine, series_key, now)
                except Exception:  # noqa: BLE001 - a reading for the ladder, never a dependency
                    log.exception("desk: the stored minute bars of %s could not be read", key)
                    continue
                t = self.trackers.get(key)
                if t is not None:
                    t.minute_history = bars  # one assignment: the old or the new, whole

        at = now.astimezone(IST).time()
        if dt.time(9, 0) <= at <= dt.time(15, 35):
            self._minute_loader = threading.Thread(target=load, name="minute-history", daemon=True)
            self._minute_loader.start()
        else:
            load()

    def _maybe_switch_source(self) -> None:
        want_dhan = self.cfg.source == "dhan" or (
            self.cfg.source == "auto" and bool(self._data_api_active())
        )
        has_dhan = self.source.primary is not None
        adapter = self._adapter_getter()
        feed = self._feed_getter()
        feed_live = (
            feed is not None
            and getattr(feed, "broker", None) is not None
            and type(feed.broker).__name__ == "DhanAdapter"
        )
        have_ws = has_dhan and getattr(self.source.primary, "name", "") == "dhan-ws"
        if want_dhan and adapter is not None and (not has_dhan or (feed_live and not have_ws)):
            self.source = build_source(
                "dhan", adapter, True, feed if feed_live else None, self._all_specs()
            )
            self._reset_bars()
            log.info("desk: switched to %s bars", self.source.name)
        elif has_dhan and (not want_dhan or adapter is None):
            self.source = build_source("yahoo", None, False)
            self._reset_bars()
            log.info("desk: switched to Yahoo bars")
        elif has_dhan and adapter is not None:
            # a token renewed in Settings builds a new adapter (REVIEW O-11): the bar source
            # reads through it from the next fetch on; the bars held are kept
            rest = getattr(self.source.primary, "rest", self.source.primary)
            swap = getattr(rest, "use_adapter", None)
            if getattr(rest, "adapter", adapter) is not adapter and callable(swap):
                swap(adapter)
                log.info("desk: the bar source now reads through the renewed adapter")

    def _reset_bars(self) -> None:
        self.fine_minutes = int(self.source.resolution[:-1])
        for t in self.trackers.values():
            t.fine_minutes = self.fine_minutes
            t.bars = pd.DataFrame()
            t.live_quote = None
            for s in t.slots:
                s.last_bar_ts = None
        self.context_bars = {}

    def _served_by(self, key: str) -> str:
        """The source that served `key` on the last poll (e.g. "yahoo" when Dhan had no bars),
        else the composite's own name."""
        return self.source.served_by.get(key, self.source.name)

    def _reset_daily_counters(self, now: dt.datetime) -> None:
        day = now.date().isoformat()
        if getattr(self, "_counter_day", None) != day:
            self._counter_day = day
            for t in self.trackers.values():
                t.trades_today = 0
                t.consecutive_losses = 0
                t.pnl_today = 0.0
                for s in t.slots:
                    s.signals_today = 0

    # ── evidence for the confidence score ─────────────────────────────────
    def _backtest_evidence(self, strategy: str, instrument: str, timeframe: str) -> dict | None:
        at, board = self._board_cache
        now = now_ist()
        if at is None or (now - at) > dt.timedelta(minutes=10):
            try:
                from ..lab.sweep import leaderboard

                rows = leaderboard(self.engine, min_trades=1, limit=5000)
                board = {}
                for r in rows:  # prefer walk-forward (OOS) rows
                    key = (r["strategy"], r["instrument"], r["timeframe"])
                    if key not in board or (r["oos"] and not board[key]["oos"]):
                        board[key] = r
            except Exception:  # noqa: BLE001
                board = {}
            self._board_cache = (now, board)
        return board.get((strategy, instrument, timeframe))

    # ── signals ───────────────────────────────────────────────────────────
    def _raise_signal(
        self, t: InstrumentTracker, slot: StrategySlot, v: Verdict, now: dt.datetime
    ) -> None:
        busy = self._busy_reason(t.instrument.key, slot.cfg.name, slot.cfg.timeframe, v)
        if busy is not None:
            log.debug(
                "desk: %s %s %s not alerted: %s",
                t.cfg.key,
                slot.cfg.name,
                slot.cfg.timeframe,
                busy,
            )
            return
        try:
            eco = self._economics(t, slot, v, now)
        except Exception as exc:  # noqa: BLE001
            log.exception("desk economics failed for %s", t.cfg.key)
            t.last_error = f"economics: {type(exc).__name__}: {exc}"
            return
        exit_time = str(getattr(slot.strategy.p, "exit_time", "15:15"))
        # A stock short is intraday only (ADR-001 am. 8): the broker squares it off at the
        # close, so it is never carried overnight whatever stock_horizon says.
        intraday = t.cfg.kind == "INDEX" or (t.cfg.kind == "STOCK" and eco.side == "SELL")
        horizon = "intraday" if intraday else self.cfg.stock_horizon
        sid = uuid.uuid4().hex[:12]
        expires = now + dt.timedelta(minutes=self.cfg.signal_valid_minutes)
        text = alert_text(
            t.series.label,
            t.series.key,
            eco,
            slot.cfg.name,
            slot.cfg.timeframe,
            v.bar_ts,
            exit_time if horizon == "intraday" else "n/a (swing: hold while the strategy holds)",
            v.reasons,
        )
        regime_now = (slot.last_features or {}).get("regime")
        regime_ok = (regime_now in getattr(slot.strategy, "regimes", ())) if regime_now else None
        conf = confidence(
            v.strength,
            self._backtest_evidence(slot.cfg.name, t.instrument.key, slot.cfg.timeframe),
            self.scoreboard.get(slot.cfg.name),
            regime_ok,
        )
        src = self._served_by(t.cfg.key)
        delayed = is_delayed_source(src)
        manage: dict[str, Any] = {
            "target_r": float(getattr(slot.strategy.p, "target_r", 0) or 0),
            "trail_atr_mult": float(getattr(slot.strategy.p, "trail_atr_mult", 0) or 0),
            "trail_activate_r": float(getattr(slot.strategy.p, "trail_activate_r", 1.0) or 1.0),
            "max_bars": int(getattr(slot.strategy.p, "max_bars", 0) or 0),
            "exit_time": exit_time,
            "atr": (slot.last_features or {}).get("atr"),
            # the trail's own ATR, frozen for the paper book (US-002 review 1 MAJOR-1)
            "atr_trail": (slot.last_features or {}).get("atr_trail"),
            "tf_min": slot.tf_min,
            "horizon": horizon,
        }
        if slot.line_trail:
            # The strategy trails the stop along its own line: the paper trade (and a position
            # the owner takes on this alert) follows the level the slot reports after each of
            # its closed bars. Absent on every other alert, so those stay exactly as they were.
            manage["line_trail"] = True
        sig = {
            "id": sid,
            "created_at": _iso(now),
            "expires_at": _iso(expires),
            "instrument_key": t.instrument.key,
            "series_key": t.series.key,
            # What to type into Dhan (US-004): universe.yaml's trading_symbol (MM -> M&M),
            # else the key itself.
            "trading_symbol": str(t.series.extra.get("trading_symbol") or t.series.key),
            "label": t.series.label,
            "kind": t.cfg.kind,
            "side": eco.side,
            "direction": v.direction,
            "strategy": slot.cfg.name,
            "strategy_label": slot.label,
            "timeframe": slot.cfg.timeframe,
            "bar_ts": _iso(v.bar_ts),
            "strength": v.strength,
            "confidence": conf,
            "horizon": horizon,
            "data": {
                "source": src,
                "resolution": f"{self.fine_minutes}m",
                "delayed": delayed,
                "delay_note": "Yahoo bars run ~15 minutes behind the exchange"
                if delayed
                else "live exchange feed",
                "bar_age_sec": t.bar_age_sec(now),
            },
            "reasons": v.reasons,
            "economics": eco.as_dict(),
            "text": f"{text}\n{slot.evidence}" if slot.evidence else text,
            "evidence": slot.evidence or None,
            "status": "PENDING",
            # no risk governor on alerts or paper trades (owner, 2026-11-07): never blocked
            "risk_allowed": True,
            "risk_reason": None,
            "outcome": None,
            "manage": manage,
        }
        slot.signals_today += 1
        self.signals[sid] = sig
        self.archive.insert(0, sig)
        with self.engine.begin() as conn:
            conn.execute(
                desk_signals.insert().values(
                    id=sid,
                    created_at=now,
                    expires_at=expires,
                    instrument_key=t.instrument.key,
                    series_key=t.series.key,
                    kind=t.cfg.kind,
                    side=eco.side,
                    strategy=slot.cfg.name,
                    timeframe=slot.cfg.timeframe,
                    bar_ts=v.bar_ts,
                    status="PENDING",
                    payload=sig,
                )
            )
        # After the desk_signals insert (a crash here is reconciled at the next start) and
        # before anyone is told: the paper trade exists before the notification goes out.
        if self.paper is not None:
            try:
                self.paper.on_alert(copy.deepcopy(sig), now)
            except Exception:  # noqa: BLE001 - the alert must go out whatever the book does
                self.hook_errors += 1
                log.exception("paper: on_alert failed for alert %s", sid)
        try:  # the alerted instrument's quote now and 1, 2, 5 minutes later (never an order)
            self.fillcost.on_alert(sig)
        except Exception:  # noqa: BLE001 - measurement never stops an alert
            log.exception("fill costs: on_alert failed for alert %s", sid)
        title = (
            f"{eco.side.replace('_', ' ')} {t.series.label}"
            + (f" {eco.option['strike']:g} {eco.option['opt_type']}" if eco.option else "")
            + (
                f" · {eco.unit_label} @ ~₹{eco.entry_price:,.2f}"
                if eco.kind == "STOCK"
                else f" · ~₹{eco.outlay:,.0f} outlay"
            )
            + f" · {slot.cfg.name} {slot.cfg.timeframe} · conf {_conf_number(conf)}"
        )
        if delayed:
            title = f"{DELAYED_TITLE_MARKER} {title}"
        self.notifier.send(
            Notification(
                title=title,
                body=str(sig["text"]),
                level=NotificationLevel.SIGNAL,
                signal_id=sid,
                ttl_seconds=self.cfg.signal_valid_minutes * 60,
                payload=sig,
            ),
            dedup_key=f"desk:{sid}",
        )
        self.version += 1

    def _busy_reason(
        self, instrument_key: str, strategy: str, timeframe: str, v: Verdict
    ) -> str | None:
        """Why a fresh entry on this instrument is not alerted, or None (OF-5).

        One live idea per instrument, so the same alert is never sent again while it runs:
        an alert still pending, a taken position the desk still watches, or a paper trade
        still waiting or open (its exit alert has not come). And never the same bar twice (a
        restart evaluates the last bar again). No risk governor and no per-symbol cap.
        """
        if any(s["instrument_key"] == instrument_key for s in self.signals.values()):
            return "an alert on it is still pending"
        if any(
            p["instrument_key"] == instrument_key and not p.get("session_over")
            for p in self.positions.values()
        ):
            return "a taken position on it is still open"
        if self.paper is not None:
            try:
                active = self.paper.open_trades()
            except Exception:  # noqa: BLE001 - the book never stops an alert
                self.hook_errors += 1
                log.exception("paper: open_trades() failed")
                active = []
            if any(tr.instrument_key == instrument_key for tr in active):
                return "its paper trade has not exited yet"
        bar = _iso(v.bar_ts)
        if bar is not None and any(
            s.get("instrument_key") == instrument_key
            and s.get("strategy") == strategy
            and s.get("timeframe") == timeframe
            and s.get("bar_ts") == bar
            and s.get("direction") == v.direction
            for s in self.archive
        ):
            return "this bar was already alerted"
        return None

    def _economics(
        self, t: InstrumentTracker, slot: StrategySlot, v: Verdict, now: dt.datetime
    ) -> Economics:
        target_r = float(getattr(slot.strategy.p, "target_r", 0) or 0)
        if t.cfg.kind == "STOCK":
            return stock_economics(
                t.instrument, v.direction, float(v.close), float(v.stop), target_r
            )
        chain = self._live_chain(t, now)
        if chain is not None:
            try:
                eco = index_option_economics_from_chain(
                    t.instrument, v.direction, float(v.close), float(v.stop), target_r, chain, now
                )
                if eco is not None:
                    if eco.option is not None:  # where its own bars live (real premiums)
                        eco.option.setdefault("segment", f"{t.series.exchange}_FNO")
                        eco.option.setdefault("underlying", t.series.key)
                    return eco
            except Exception:  # noqa: BLE001
                log.exception("chain economics failed; using the synthetic model")
        vix = self._context_last("INDIAVIX")
        return index_option_economics(
            t.instrument, v.direction, float(v.close), float(v.stop), target_r, now, vix
        )

    def _live_chain(self, t: InstrumentTracker, now: dt.datetime):
        """The option chain of the contract an index alert names (desk.yaml `option_expiry`:
        the Lab's monthly contract by default, rolled inside `option_roll_days`), cached
        60 s (1 request / 3 s limit)."""
        adapter = self._adapter_getter()
        if adapter is None or not self._data_api_active():
            return None
        sid = t.series.dhan_security_id
        seg = t.series.dhan_segment
        if not sid:
            return None
        hit = self._chain_cache.get(t.cfg.key)
        if hit and (now - hit[0]).total_seconds() < 60:
            return hit[1]
        try:
            expiries = adapter.expiry_list_raw(int(sid), seg) or []
            expiry = pick_expiry(
                expiries, now.date(), self.cfg.option_expiry, self.cfg.option_roll_days
            )
            if expiry is None:
                return None
            snap = adapter.option_chain(t.series.key, expiry, int(sid), seg)
        except Exception as exc:  # noqa: BLE001
            log.warning("option chain for %s unavailable: %s", t.cfg.key, exc)
            self._chain_cache[t.cfg.key] = (now, None)
            return None
        self._chain_cache[t.cfg.key] = (now, snap)
        return snap

    # ── real option prices for the paper book (paper test, owner 2026-11-07) ──────────
    def real_premium_at(self, option: dict, underlying: float, ts: dt.datetime) -> float | None:
        """The alerted option's TRADED price when its index stood at `underlying` at `ts`:
        the contract's own one-minute bars (Dhan intraday, read again when the wanted minute
        is not in them yet, at most every 5 s)
        against the index's one-minute bars (`lab.real_options.premium_in_minute`). None
        whenever either is missing (Yahoo bars, no Data API, no trade in that minute), and
        the paper book's model prices that leg instead."""
        sid, seg, und = option.get("security_id"), option.get("segment"), option.get("underlying")
        opt = option.get("opt_type")
        if not (sid and seg and und) or opt not in ("CE", "PE"):
            return None
        t = self.trackers.get(str(und))
        if t is None or t.fine_minutes != 1 or t.bars.empty or not self._data_api_active():
            return None
        bars = self._contract_bars(str(sid), str(seg), str(und), ts)
        if bars is None or bars.empty:
            return None
        return premium_in_minute(bars, t.bars, float(underlying), ts, str(opt))

    def _contract_bars(self, sid: str, seg: str, und: str, ts: dt.datetime) -> pd.DataFrame | None:
        now = now_ist()
        want = pd.Timestamp(ts).tz_convert(IST).floor("min")
        hit = self._contract_cache.get(sid)
        if hit is not None:
            fetched_at, df = hit
            covered = not df.empty and df.index[-1] >= want
            if covered or (now - fetched_at).total_seconds() < 5:  # at most 1 read / 5 s
                return df
        adapter = self._adapter_getter()
        if adapter is None:
            return hit[1] if hit else None
        day = min(want.to_pydatetime(), now)
        start = day.replace(hour=9, minute=0, second=0, microsecond=0)
        contract = Contract(
            security_id=sid, exchange_segment=seg, symbol=sid, underlying=und,
            instrument_type="OPTIDX",
        )  # fmt: skip
        try:
            got = adapter.historical_intraday(contract, 1, start, now)
        except Exception as exc:  # noqa: BLE001 - the model prices it instead
            log.debug("desk: bars of option %s unavailable: %s", sid, exc)
            self._contract_cache[sid] = (now, hit[1] if hit else pd.DataFrame())
            return hit[1] if hit else None
        # only FINISHED minutes: Dhan's answer can hold the minute still forming, whose
        # candle would later pass for that minute's final one (DhanBars drops it too)
        rows = [
            {"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for b in got
            if b.ts + dt.timedelta(minutes=1) <= now
        ]
        df = pd.DataFrame(rows).set_index("ts").sort_index() if rows else pd.DataFrame()
        if len(self._contract_cache) > 64:  # bounded: a day's alerted contracts
            self._contract_cache.pop(next(iter(self._contract_cache)))
        self._contract_cache[sid] = (now, df)
        return df

    def _expire_signals(self, now: dt.datetime) -> None:
        for sid, s in list(self.signals.items()):
            if dt.datetime.fromisoformat(s["expires_at"]) <= now:
                self._resolve_signal(sid, "EXPIRED")

    def _resolve_signal_db(self, sid: str, status: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(desk_signals)
                .where(desk_signals.c.id == sid)
                .values(status=status, resolved_at=now_ist())
            )

    def _resolve_signal(self, sid: str, status: str, note: str | None = None) -> None:
        s = self.signals.pop(sid, None)
        now = now_ist()
        for r in self.archive:  # memory first: a failed write below still resolves the card
            if r["id"] == sid:
                r["status"] = status
                r["resolved_at"] = _iso(now)
        self._resolve_signal_db(sid, status)
        if s and self.repo is not None:
            try:
                self.repo.event(now, "INFO", "desk_signal", f"{sid} {status}", {"note": note})
            except Exception:  # noqa: BLE001
                pass
        self.version += 1

    def acknowledge(
        self, sid: str, action: str, fill_price: float | None = None, source: str = "dashboard"
    ) -> dict:
        with self._lock:
            s = self.signals.get(sid)
            if s is None:
                return {
                    "accepted": False,
                    "message": "signal not pending (expired or already resolved)",
                }
            act = action.upper()
            if act in ("SKIP", "SKIPPED"):
                self._resolve_signal(sid, "SKIPPED")
                return {"accepted": True, "message": "skipped and archived"}
            if act not in ("TAKE", "TAKEN"):
                return {"accepted": False, "message": "action must be TAKE or SKIP"}
            now = now_ist()
            eco = s["economics"]
            t = self.trackers.get(s["series_key"])
            # The alert's price is the record (OF-5); what the owner typed is kept beside it
            # so the difference shows, never in its place.
            entry = float(eco["entry_price"])
            user_entry = _reported(fill_price)
            pid = uuid.uuid4().hex[:12]
            pos = {
                "id": pid,
                "signal_id": sid,
                "instrument_key": s["instrument_key"],
                "series_key": s["series_key"],
                "trading_symbol": s.get("trading_symbol") or s["series_key"],  # older alerts
                "label": s["label"],
                "kind": s["kind"],
                "side": s["side"],
                "direction": s["direction"],
                "strategy": s["strategy"],
                "timeframe": s["timeframe"],
                "opened_at": _iso(now),
                "qty": int(eco["qty"]),
                "entry_price": entry,
                "alert_entry_price": entry,
                "user_entry_price": user_entry,
                "alert_created_at": s.get("created_at"),
                "entry_underlying": float(eco["underlying_entry"]),
                "stop_underlying": float(eco["underlying_stop"]),
                "target_underlying": eco.get("underlying_target"),
                "initial_stop_underlying": float(eco["underlying_stop"]),
                "option": eco.get("option") or {},
                "manage": s["manage"],
                "horizon": s.get("horizon", "intraday"),
                "trail_active": False,
                "bars_held": 0,
                "sessions_held": 0,
                "last_underlying": float(eco["underlying_entry"]),
                "unrealized": 0.0,
                "r_now": 0.0,
                "exit_advice": None,
                "hold_check": "monitoring",
                "status": "OPEN",
                "source": source,
                "notes": list(eco.get("notes") or []),
                "confidence": s.get("confidence"),
            }
            self.positions[pid] = pos
            if t is not None:
                t.trades_today += 1
            with self.engine.begin() as conn:
                conn.execute(
                    desk_positions.insert().values(
                        id=pid,
                        signal_id=sid,
                        instrument_key=pos["instrument_key"],
                        series_key=pos["series_key"],
                        kind=pos["kind"],
                        side=pos["side"],
                        strategy=pos["strategy"],
                        timeframe=pos["timeframe"],
                        opened_at=now,
                        status="OPEN",
                        entry_price=entry,
                        qty=pos["qty"],
                        payload=pos,
                    )
                )
            self._resolve_signal(sid, "TAKEN")
            yours = f" · you reported {user_entry:,.2f}" if user_entry is not None else ""
            self.notifier.info(
                f"Monitoring {s['label']} {s['side'].replace('_', ' ')}",
                f"alert entry {entry:,.2f}{yours} · stop {pos['stop_underlying']:,.2f} "
                f"(underlying) · {pos['horizon']} · it closes at the alert's exit price",
                dedup_key=f"desk:open:{pid}",
            )
            return {
                "accepted": True,
                "message": "position opened; the desk is monitoring it",
                "position_id": pid,
            }

    # ── positions ─────────────────────────────────────────────────────────
    def _manage_positions(self, now: dt.datetime, window) -> None:
        for pid, p in list(self.positions.items()):
            t = self.trackers.get(p["series_key"])
            if t is None or t.bars.empty:
                continue
            last = t.last_price()
            if last is None:
                continue
            sign = 1 if p["direction"] == "long" else -1
            m = p["manage"]
            horizon = p.get("horizon", "intraday")
            risk_pts = abs(p["entry_underlying"] - p["initial_stop_underlying"]) or 1e-9
            r_now = sign * (last - p["entry_underlying"]) / risk_pts
            p["last_underlying"], p["r_now"] = last, round(r_now, 3)
            p["unrealized"] = round(self._unrealized(p, last, now), 2)
            if m.get("trail_atr_mult") and r_now >= float(m.get("trail_activate_r", 1.0)):
                atr = m.get("atr") or t.regime.get("atr") or 0.0
                if atr:
                    new_stop = last - sign * float(m["trail_atr_mult"]) * float(atr)
                    if (sign > 0 and new_stop > p["stop_underlying"]) or (
                        sign < 0 and new_stop < p["stop_underlying"]
                    ):
                        p["stop_underlying"], p["trail_active"] = round(new_stop, 2), True
            opened = dt.datetime.fromisoformat(p["opened_at"])
            slot = _slot_of(t, p["strategy"], p["timeframe"])
            if m.get("line_trail") and slot is not None and slot.last_bar_ts is not None:
                # The strategy's own line (its Supertrend) is the trail: after each closed
                # strategy bar the stop moves to the level the line allows, and never back. A
                # level from a bar that closed before the position was opened is its first stop.
                level = slot.trail_level(p["direction"])
                closed_at = slot.last_bar_ts + dt.timedelta(minutes=slot.tf_min)
                if (
                    level is not None
                    and closed_at > opened
                    and sign * (level - p["stop_underlying"]) > 0
                ):
                    p["stop_underlying"], p["trail_active"] = round(level, 2), True
            p["bars_held"] = int(
                max(0, (now - opened).total_seconds() // 60) // max(1, int(m.get("tf_min", 5)))
            )
            p["sessions_held"] = len(
                {
                    d
                    for d in t.bars.index.normalize().unique()
                    if d.to_pydatetime() >= opened.replace(hour=0, minute=0)
                }
            )
            strat_exit = slot.exit_advice(p["direction"]) if slot else None
            advice = None
            if (sign > 0 and last <= p["stop_underlying"]) or (
                sign < 0 and last >= p["stop_underlying"]
            ):
                advice = "STOP" if not p["trail_active"] else "TRAIL_STOP"
            elif p.get("target_underlying") and (
                (sign > 0 and last >= p["target_underlying"])
                or (sign < 0 and last <= p["target_underlying"])
            ):
                advice = "TARGET"
            elif strat_exit:
                advice = strat_exit
            elif horizon == "intraday" and window is not None:
                hh, mm = (int(x) for x in str(m.get("exit_time", "15:15")).split(":"))
                if now.time() >= dt.time(hh, mm):
                    advice = "SQUARE_OFF"
            if (
                advice is None
                and horizon == "intraday"
                and m.get("max_bars")
                and p["bars_held"] >= int(m["max_bars"])
            ):
                advice = "TIME_STOP"
            if (
                advice is None
                and horizon == "swing"
                and p["sessions_held"] > int(self.cfg.swing_max_sessions)
            ):
                advice = "TIME_STOP"
            p["hold_check"] = f"strategy {p['strategy']} {p['timeframe']}: " + (
                "still " + ("long" if sign > 0 else "short") + " — HOLD"
                if not strat_exit
                else strat_exit.replace("_", " ").lower()
            )
            linked = self._paper_link(p)
            if linked is UNREADABLE:
                advice = None  # never a fallback exit on a book that could not be read
                p["hold_check"] += " · the paper book could not be read this poll"
            elif linked is not None:
                # The paper trade's close is this alert's one exit (OF-5): the desk gives no
                # advice of its own and sends nothing; _step_paper closes the position at the
                # exit alert's price.
                advice = None
                p["paper_trade_id"], p["paper_status"] = linked.id, linked.status
                p["hold_check"] += (
                    f" · exit alert from paper trade #{linked.id}"
                    f" ({str(linked.status).replace('_', ' ').lower()})"
                )
            elif horizon == "intraday" and to_ist(opened).date() < to_ist(now).date():
                advice = None  # the broker squared it off at the close: no trading advice
                self._note_session_over(pid, p)
            p["exit_advice"] = advice
            if advice and self._fallback_exit(pid, p, advice, last, r_now, now):
                continue  # closed at its exit alert's price; the close stored the row
            with self.engine.begin() as conn:
                conn.execute(
                    update(desk_positions).where(desk_positions.c.id == pid).values(payload=p)
                )

    def _note_session_over(self, pid: str, p: dict) -> None:
        """An intraday position from an earlier session (US-004 decision E): it raises no EXIT
        (no trading advice, no phone reminders) and the desk stops watching it. The broker
        squares off an intraday (MIS) position at the close, but not one bought NRML (an option
        held past the close), so the note covers both. The card says so, and the owner is told
        once, as an INFO (INFO never reaches Telegram); the flag is in the stored payload, so a
        restart does not say it again."""
        p["hold_check"] = SESSION_OVER_NOTE
        if p.get("session_over"):
            return
        p["session_over"] = True
        self.notifier.info(
            f"Session over: {p['label']} {p['side'].replace('_', ' ')}",
            "This intraday position is from an earlier session. If the broker squared it off "
            "at the close, record the square-off price with Closed. If you still hold it, the "
            "desk no longer watches it: close it on your terminal, then record the exit with "
            "Closed.",
            dedup_key=f"desk:session_over:{pid}",
        )

    def _paper_link(self, p: dict) -> Any:
        """The paper trade of a taken position's alert, in any state; None when the alert has
        none (an alert from before the paper book, or paper off); UNREADABLE when the book
        could not be read this poll (asked again next poll: alert_id is unique)."""
        sid = p.get("signal_id")
        if self.paper is None or not sid:
            return None
        try:
            return self.paper.for_alert(sid)
        except Exception:  # noqa: BLE001 - the book never stops the desk
            self.hook_errors += 1
            log.exception("paper: for_alert(%s) failed", sid)
            return UNREADABLE

    def _mark_price(self, p: dict, now: dt.datetime, last: float | None = None) -> float | None:
        """What the desk would book this position at now: a stock at its last price, an
        option at the model premium for the last underlying. None when nothing is known
        (never an invented price)."""
        if last is None:
            t = self.trackers.get(p["series_key"])
            last = t.last_price() if t is not None else None
        if last is None:
            return None
        if p["kind"] == "STOCK":
            return round(float(last), 2)
        o = p.get("option") or {}
        prem = self._premium_at(o, last, now) if o else None
        if prem is None or not math.isfinite(prem) or prem <= 0:
            return None
        return round(float(prem), 2)

    def _fallback_exit(
        self, pid: str, p: dict, advice: str, last: float, r_now: float, now: dt.datetime
    ) -> bool:
        """A taken position whose alert has no paper trade: the desk's own advice is the
        alert's one exit (OF-5). It is told ONCE (the flag is stored with the position) and
        the position closes at the desk's price; with no price it stays open, flagged, for
        the owner to close with their own price. True when it closed."""
        price = self._mark_price(p, now, last)
        sig = self._archived(p.get("signal_id"))
        if sig is not None and (sig.get("exit") or sig.get("exit_told")):
            p["exit_alert_sent"] = p.get("exit_alert_sent") or advice  # already told once
        if not p.get("exit_alert_sent"):
            p["exit_alert_sent"] = advice
            self._mark_exit_told(sig, advice)
            closing = (
                f"Closed here at {price:,.2f}; report your own exit to compare."
                if price is not None
                else "No price is known here: close it at the market and record your exit."
            )
            self.notifier.send(
                Notification(
                    title=f"EXIT {p['label']} — {advice.replace('_', ' ')}",
                    body=(
                        f"{p['side'].replace('_', ' ')} from {p['entry_price']:,.2f}; underlying "
                        f"now {last:,.2f} ({r_now:+.2f}R, est. {p['unrealized']:+,.0f}). {closing}"
                    ),
                    level=NotificationLevel.EXIT,
                    position_id=pid,
                    payload={"advice": advice, "position": p},
                ),
                dedup_key=f"desk:exit:{pid}",
            )
        if price is None:
            return False
        self._close(pid, reason=advice, source="desk", alert_exit=price, now=now)
        return True

    def _unrealized(self, p: dict, last: float, now: dt.datetime) -> float:
        sign = 1 if p["direction"] == "long" else -1
        if p["kind"] == "STOCK":
            return sign * (last - p["entry_price"]) * p["qty"]
        o = p.get("option") or {}
        if not o:
            return 0.0
        prem = self._premium_at(o, last, now)
        return (prem - p["entry_price"]) * p["qty"] if prem is not None else 0.0

    def _premium_at(self, o: dict, underlying: float, when) -> float | None:
        from ..lab.execution import OptionPricer, expiry_datetime

        try:
            vix = self._context_last("INDIAVIX")
            ts = pd.Timestamp(when)
            ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            pricer = OptionPricer(
                self.app_cfg.backtest, pd.Series([vix], index=[ts.normalize()]) if vix else None
            )
            exp = o.get("expiry")
            if not exp:
                return None
            return pricer.premium(
                underlying,
                float(o["strike"]),
                ts,
                expiry_datetime(dt.date.fromisoformat(str(exp)[:10])),
                o["opt_type"],
            )
        except Exception:  # noqa: BLE001
            return None

    def close_position(self, pid: str, exit_price: float, reason: str = "MANUAL") -> dict | None:
        """The owner closed a taken position themselves, before its exit alert, at their own
        price: kept as their reported exit. The alert's exit price is added to the closed
        trade when the alert's exit comes (OF-5), so the two can be compared. None when there
        is no such open position."""
        price = _reported(exit_price)
        if price is None:
            if pid not in self.positions:
                return None
            return {
                "closed": False,
                "message": "type the price you exited at: above zero and below "
                f"{MAX_REPORTED_PRICE:,.0f}",
            }
        return self._close(pid, reason=reason, source="user", user_exit=price)

    def _close(
        self,
        pid: str,
        *,
        reason: str,
        source: str,
        alert_exit: float | None = None,
        user_exit: float | None = None,
        now: dt.datetime | None = None,
    ) -> dict | None:
        """Close a taken position: at its exit alert's price (`source` "alert": the paper
        trade's close; "desk": the desk's own advice for an alert without one) or at the
        owner's price ("user"). One close per position: None if it is not open."""
        with self._lock:
            p = self.positions.pop(pid, None)
            if p is None:
                return None
            now = now or now_ist()
            if alert_exit is not None:
                p["alert_exit_price"] = float(alert_exit)
            if user_exit is not None:
                p["user_exit_price"] = float(user_exit)
            p.update(
                {
                    "status": "CLOSED",
                    "closed_at": _iso(now),
                    "exit_reason": reason,
                    "exit_source": source,
                }
            )
            self._settle(p)
            exit_price = float(p["exit_price"])
            gross, costs, net = float(p["gross_pnl"]), float(p["costs"]), float(p["net_pnl"])
            with self.engine.begin() as conn:
                conn.execute(
                    update(desk_positions)
                    .where(desk_positions.c.id == pid)
                    .values(
                        status="CLOSED",
                        closed_at=now,
                        exit_price=exit_price,
                        gross_pnl=gross,
                        costs=costs,
                        net_pnl=net,
                        exit_reason=reason,
                        payload=p,
                    )
                )
            t = self.trackers.get(p["series_key"])
            if t is not None:
                t.pnl_today += net
                t.consecutive_losses = t.consecutive_losses + 1 if net < 0 else 0
            for r in self.archive:
                if r["id"] == p.get("signal_id"):
                    r["status"] = "CLOSED"
                    r["trade"] = _trade_brief(p)
            whose = {
                "user": "your price",
                "desk": "the desk's last price",
            }.get(source, "the exit alert's price")
            self.notifier.info(
                f"Closed {p['label']}: {net:+,.0f} net",
                f"{reason.replace('_', ' ')} at {exit_price:,.2f} ({whose}); costs {costs:,.0f}",
                dedup_key=f"desk:close:{pid}",
            )
            self.version += 1
            return {
                "closed": True,
                "exit_price": exit_price,
                "net_pnl": round(net, 2),
                "gross_pnl": round(gross, 2),
                "costs": round(costs, 2),
                "user_net_pnl": p.get("user_net_pnl"),
                "diff_pnl": p.get("diff_pnl"),
            }

    def report_prices(
        self, pid: str, entry_price: float | None = None, exit_price: float | None = None
    ) -> dict | None:
        """The owner's own fill prices for a taken position, open or closed (OF-5). They are
        kept beside the alert's prices, never in their place, so the difference shows. None
        when there is no such position."""
        entry, exit_ = _reported(entry_price), _reported(exit_price)
        if entry is None and exit_ is None:
            return {
                "ok": False,
                "message": "give your entry or exit price: above zero and below "
                f"{MAX_REPORTED_PRICE:,.0f}",
            }
        with self._lock:
            p = self.positions.get(pid)
            if p is not None:
                if entry is not None:
                    p["user_entry_price"] = entry
                if exit_ is not None:
                    p["user_exit_price"] = exit_
                with self.engine.begin() as conn:
                    conn.execute(
                        update(desk_positions).where(desk_positions.c.id == pid).values(payload=p)
                    )
                self.version += 1
                return {"ok": True, "status": "OPEN", **_prices(p)}
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(desk_positions).where(desk_positions.c.id == pid))
                .mappings()
                .first()
            )
        if row is None:
            return None
        p = dict(row["payload"] or {})
        p.setdefault("id", pid)
        p["status"] = row["status"]
        if entry is not None:
            p["user_entry_price"] = entry
        if exit_ is not None:
            p["user_exit_price"] = exit_
        stored = self._store_settled(pid, p, closed=row["status"] == "CLOSED")
        return {"ok": True, "status": row["status"], **_prices(stored)}

    def _store_settled(self, pid: str, p: dict, closed: bool) -> dict:
        """Write the prices `p` carries (PRICE_PATCH) onto the stored position, recompute its
        money and store it; the archive's trade brief follows. Returns the stored payload.

        Read-modify-write under the desk lock on a FRESH read of the row: the owner's report
        (API thread) and the alert's exit (poll thread) both land, whichever came first; a
        stale copy can never wipe the other's price (QA cycle 1, BUG-5)."""
        with self._lock:
            with self.engine.begin() as conn:
                row = (
                    conn.execute(
                        select(desk_positions.c.payload, desk_positions.c.status).where(
                            desk_positions.c.id == pid
                        )
                    )
                    .mappings()
                    .first()
                )
                fresh = dict((row["payload"] if row is not None else None) or p)
                fresh |= {k: p[k] for k in PRICE_PATCH if p.get(k) is not None}
                fresh.setdefault("id", pid)
                if row is not None:
                    fresh["status"] = row["status"]
                self._settle(fresh)
                values: dict[str, Any] = {"payload": fresh}
                if closed and fresh.get("net_pnl") is not None:
                    values |= {
                        "exit_price": fresh["exit_price"],
                        "gross_pnl": fresh["gross_pnl"],
                        "costs": fresh["costs"],
                        "net_pnl": fresh["net_pnl"],
                    }
                conn.execute(
                    update(desk_positions).where(desk_positions.c.id == pid).values(**values)
                )
            for r in self.archive:
                if r["id"] == fresh.get("signal_id") and r.get("trade") is not None:
                    r["trade"] = _trade_brief(fresh)
            self.version += 1
        return fresh

    def _settle(self, p: dict) -> None:
        """A taken position's money from its four prices (OF-5). The alert's entry and exit
        are the record; the owner's reported prices, where given, make `user_net_pnl` and
        `diff_pnl` (theirs minus the alert's). The headline (`exit_price`, `gross_pnl`,
        `costs`, `net_pnl`) is the alert's once its exit is known, else the owner's. A
        position closed before OF-5 was closed at the owner's price: that price is theirs."""
        entry = float(p.get("alert_entry_price") or p["entry_price"])
        alert_exit = p.get("alert_exit_price")
        user_entry, user_exit = p.get("user_entry_price"), p.get("user_exit_price")
        if (
            p.get("status") == "CLOSED"
            and alert_exit is None
            and user_exit is None
            and p.get("exit_price") is not None
        ):
            user_exit = p["user_exit_price"] = float(p["exit_price"])
        alert = self._pnl(p, entry, float(alert_exit)) if alert_exit is not None else None
        theirs = None
        if user_entry is not None or user_exit is not None:
            out = user_exit if user_exit is not None else alert_exit
            if out is not None:
                into = float(user_entry) if user_entry is not None else entry
                theirs = self._pnl(p, into, float(out))
        p["alert_net_pnl"] = round(alert[2], 2) if alert else None
        p["user_net_pnl"] = round(theirs[2], 2) if theirs else None
        p["diff_pnl"] = round(theirs[2] - alert[2], 2) if alert and theirs else None
        head, head_exit = (alert, alert_exit) if alert else (theirs, user_exit)
        if head is not None and head_exit is not None:
            p["exit_price"] = float(head_exit)
            p["gross_pnl"], p["costs"], p["net_pnl"] = (round(x, 2) for x in head)

    def _pnl(self, p: dict, entry: float, exit_: float) -> tuple[float, float, float]:
        """(gross, costs, net) in rupees of one unit (one lot or one share) from entry to
        exit: a stock by its direction, an option as long premium."""
        qty = int(p["qty"])
        if p["kind"] == "STOCK":
            sign = 1 if p["direction"] == "long" else -1
            gross = sign * (exit_ - entry) * qty
        else:
            gross = (exit_ - entry) * qty
        costs = 0.0
        inst = self.instruments.get(p["instrument_key"])
        if inst is not None:
            from .economics import _costs_for

            try:
                costs = float(_costs_for(inst).round_trip_cost(entry, exit_, qty))
            except Exception:  # noqa: BLE001
                costs = 0.0
        return gross, costs, gross - costs

    def journal(self, limit: int = 200) -> list[dict]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(desk_positions)
                    .where(desk_positions.c.status == "CLOSED")
                    .order_by(desk_positions.c.closed_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        out = []
        for r in rows:
            d = {
                k: r[k]
                for k in (
                    "id",
                    "instrument_key",
                    "series_key",
                    "kind",
                    "side",
                    "strategy",
                    "timeframe",
                    "entry_price",
                    "exit_price",
                    "qty",
                    "gross_pnl",
                    "costs",
                    "net_pnl",
                    "exit_reason",
                )
            }
            d["opened_at"], d["closed_at"] = (
                _iso(_aware(r["opened_at"])),
                _iso(_aware(r["closed_at"])),
            )
            payload = r["payload"] or {}
            d |= _prices(payload)
            for k, v in d.items():  # JSON cannot carry inf or nan: such a figure is unknown
                if isinstance(v, float) and not math.isfinite(v):
                    d[k] = None
            d["alert_entry_price"] = payload.get("alert_entry_price") or r["entry_price"]
            for k in ("signal_id", "label", "trading_symbol", "alert_created_at", "exit_source"):
                d[k] = payload.get(k)
            if d["user_exit_price"] is None and not payload.get("exit_source"):
                d["user_exit_price"] = r["exit_price"]  # closed before OF-5: at the owner's price
            out.append(d)
        # positions taken before OF-5 do not carry their alert's time: read it from the alert
        missing = {d["signal_id"] for d in out if not d["alert_created_at"] and d["signal_id"]}
        if missing:
            with self.engine.connect() as conn:
                found = {
                    sid: _iso(_aware(at))
                    for sid, at in conn.execute(
                        select(desk_signals.c.id, desk_signals.c.created_at).where(
                            desk_signals.c.id.in_(missing)
                        )
                    )
                }
            for d in out:
                if not d["alert_created_at"]:
                    d["alert_created_at"] = found.get(d["signal_id"])
        return out

    # ── audit of archived alerts ──────────────────────────────────────────
    def _audit_archive(self, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(days=self.cfg.audit_days)
        self.archive = [
            s for s in self.archive if dt.datetime.fromisoformat(s["created_at"]) >= cutoff
        ]
        changed = []
        for s in self.archive:
            if s["status"] == "PENDING":
                continue
            prev = s.get("outcome") or {}
            if prev.get("outcome") in ("TARGET", "STOP", "SQUARE_OFF") and s["status"] != "TAKEN":
                continue  # final; nothing to recompute
            t = self.trackers.get(s["series_key"])
            if t is None or t.bars.empty:
                continue
            premium_fn = None
            if s["kind"] == "INDEX" and (s.get("economics") or {}).get("option"):
                o = s["economics"]["option"]
                premium_fn = lambda u, ts, o=o: self._premium_at(o, u, ts)  # noqa: E731
            exit_time = (s.get("manage") or {}).get("exit_time", "15:15")
            s["outcome"] = audit_signal(
                s,
                t.bars,
                now,
                self.fine_minutes,
                s.get("horizon", "intraday") == "intraday",
                exit_time,
                premium_fn,
            )
            changed.append(s)
        if changed:
            with self.engine.begin() as conn:
                for s in changed:
                    conn.execute(
                        update(desk_signals).where(desk_signals.c.id == s["id"]).values(payload=s)
                    )
        self.scoreboard = strategy_scoreboard(self.archive)

    def signals_view(self, days: int = 7) -> dict:
        cutoff = now_ist() - dt.timedelta(days=days)
        rows = [s for s in self.archive if dt.datetime.fromisoformat(s["created_at"]) >= cutoff]
        delivery: list[dict] = []
        if self.delivery is not None:
            try:
                delivery = self.delivery.alerts(cutoff)
            except Exception:  # noqa: BLE001 - the intraday alerts still answer
                log.exception("delivery: the alert archive could not be read")
        return {
            "signals": rows,
            "scoreboard": self.scoreboard,
            "days": days,
            "pending": [s["id"] for s in self.signals.values()],
            "delivery_alerts": delivery,
        }

    def bars_view(self, key: str, timeframe: str | None = None, n: int = 300) -> dict:
        t = self.trackers.get(key.upper())
        if t is None:
            return {"key": key, "bars": [], "markers": []}
        tf = timeframe or f"{self.fine_minutes}m"
        df = t.bars_for(tf, n)
        bars = [
            [int(ts.timestamp()), float(o), float(h), float(l), float(c), float(v)]
            for ts, o, h, l, c, v in zip(
                df.index, df["open"], df["high"], df["low"], df["close"], df["volume"], strict=True
            )
        ]
        first = df.index[0] if not df.empty else None
        markers = []
        for s in self.archive:
            if s["instrument_key"] != t.instrument.key or not s.get("bar_ts"):
                continue
            bt = pd.Timestamp(s["bar_ts"])
            if first is not None and bt < first:
                continue
            markers.append(
                {
                    "ts": int(bt.timestamp()),
                    "side": s["direction"],
                    "strategy": s["strategy"],
                    "status": s["status"],
                    "entry": (s.get("economics") or {}).get("underlying_entry"),
                    "outcome": (s.get("outcome") or {}).get("outcome"),
                }
            )
        return {
            "key": t.cfg.key,
            "label": t.series.label,
            "timeframe": tf,
            "bars": bars,
            "markers": markers,
            "source": self._served_by(t.cfg.key),
            "live": t.live_quote,
        }

    # ── context strip ─────────────────────────────────────────────────────
    def set_context(
        self, key: str, last: float, change_pct: float | None = None, note: str | None = None
    ) -> dict:
        key = key.upper()
        self.context_manual[key] = {
            "last": float(last),
            "change_pct": change_pct,
            "note": note,
            "as_of": _iso(now_ist()),
        }
        self._wake.set()
        return self.context_manual[key]

    def _context_last(self, key: str) -> float | None:
        df = self.context_bars.get(key)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
        t = self.trackers.get(key)
        if t is not None and t.last_price() is not None:
            return t.last_price()
        m = self.context_manual.get(key)
        return float(m["last"]) if m else None

    def _context_rows(self) -> list[dict]:
        rows = []
        quotes = getattr(self.source.primary, "quotes", None) or {}
        for c in self.cfg.context:
            row: dict[str, Any] = {
                "key": c.key,
                "label": c.label,
                "manual": c.manual,
                "note": c.note,
                "last": None,
                "change_pct": None,
                "prev_close": None,
                "as_of": None,
                "source": None,
            }
            df = self.context_bars.get(c.key)
            if df is None and c.key in self.trackers:
                df = self.trackers[c.key].bars
            if df is not None and not df.empty:
                day = df.index.normalize()
                closes = df["close"].groupby(day).last()
                row["last"] = float(closes.iloc[-1])
                if len(closes) >= 2:
                    row["prev_close"] = float(closes.iloc[-2])
                row["as_of"] = _iso(df.index[-1].to_pydatetime())
                row["source"] = self._served_by(c.key)
            q = quotes.get(c.key)
            if q and q.get("ltp"):
                row["last"], row["as_of"], row["source"] = float(q["ltp"]), q.get("ts"), "dhan-ws"
            if row["last"] is not None and row["prev_close"]:
                row["change_pct"] = (row["last"] / row["prev_close"] - 1) * 100
            m = self.context_manual.get(c.key)
            if m:
                row.update(
                    {
                        "last": m["last"],
                        "change_pct": m.get("change_pct"),
                        "as_of": m.get("as_of"),
                        "source": "manual",
                        "note": m.get("note") or c.note,
                    }
                )
            rows.append(row)
        return rows

    # ── paper book (US-001) ───────────────────────────────────────────────
    def _paper_bars(self, series_key: str) -> pd.DataFrame | None:
        """The bars the book fills from: a tracker's own frame (read-only), else None."""
        tracker = self.trackers.get(series_key)
        return tracker.bars if tracker is not None else None

    def _tell_paper_tracked(self) -> None:
        """Tell the book which series this desk tracks: a waiting paper trade on any other
        series gets no bars, so the book retires it on the wall clock (review 1, MINOR-4).
        A duck-typed book without retire_untracked is left alone."""
        retire = getattr(self.paper, "retire_untracked", None)
        if retire is None:
            return
        try:
            events = retire(set(self.trackers), now_ist())
        except Exception:  # noqa: BLE001 - the desk runs whatever the book does
            self.hook_errors += 1
            log.exception("paper: retire_untracked failed")
            return
        self._paper_exits(events if isinstance(events, list) else [])

    def _paper_strategy_exit(self, trade: Any, now: dt.datetime) -> tuple[str, dt.datetime] | None:
        """For the paper book (US-002): what the trade's own strategy says on its last
        CLOSED bar, the same `exit_advice` the desk shows for a manual position, as
        (SIGNAL_EXIT | OPPOSITE_SIGNAL, that strategy bar's close time), or None.

        Read-only, and a plain tuple: the desk never imports the paper package."""
        tracker = self.trackers.get(trade.series_key)
        if tracker is None:
            return None
        slot = _slot_of(tracker, trade.strategy, trade.timeframe)
        if slot is None or slot.last_bar_ts is None:
            return None
        reason = slot.exit_advice(trade.direction)
        if reason is None:
            return None
        return reason, slot.last_bar_ts + dt.timedelta(minutes=slot.tf_min)

    def _tell_paper_trail(self) -> None:
        """Let the book's open trades follow a strategy's own line (its Supertrend): the book
        asks `_paper_trail_level` about every trade whose alert said `manage.line_trail`.
        A duck-typed book without follow_trail is left alone."""
        follow = getattr(self.paper, "follow_trail", None)
        if not callable(follow):
            return
        try:
            follow(self._paper_trail_level)
        except Exception:  # noqa: BLE001 - the desk runs whatever the book does
            self.hook_errors += 1
            log.exception("paper: follow_trail failed")

    def _paper_trail_level(self, trade: Any, now: dt.datetime) -> tuple[float, dt.datetime] | None:
        """For the paper book: where the trade's own strategy lets its stop stand after its
        last CLOSED bar, as (level in rupees of the underlying, that bar's close time), or
        None while the line gives no level (it is on the other side, or the slot is gone).

        The book moves the stop there from the first fine bar that opens at or after that
        close, and only ever towards the price: the Lab simulator's rule. Read-only, and a
        plain tuple: the desk never imports the paper package."""
        tracker = self.trackers.get(trade.series_key)
        if tracker is None:
            return None
        slot = _slot_of(tracker, trade.strategy, trade.timeframe)
        if slot is None or slot.last_bar_ts is None:
            return None
        level = slot.trail_level(trade.direction)
        if level is None:
            return None
        return level, slot.last_bar_ts + dt.timedelta(minutes=slot.tf_min)

    def _step_paper(self, now: dt.datetime) -> None:
        """Step the paper book; each trade it closes (or that never filled) is its alert's
        one exit (OF-5): told once, and a position taken on that alert closes at the exit
        alert's price."""
        if self.paper is None:
            return
        try:
            events = list(self.paper.step(now, self._paper_bars, self._paper_strategy_exit) or [])
        except Exception:  # noqa: BLE001 - the poll carries on whatever the book does
            self.hook_errors += 1
            log.exception("paper: step failed (poll %d)", self.polls)
            events = []
        self._paper_exits(events)
        try:
            self._settle_taken(now)
        except Exception:  # noqa: BLE001
            self.hook_errors += 1
            log.exception("desk: settling taken positions failed")

    def _paper_exits(self, events: Any) -> None:
        """Each paper trade the book ended (CLOSED or NO_FILL) is its alert's one exit."""
        final = dict.fromkeys(
            e.trade_id for e in events or () if getattr(e, "kind", None) in FINAL_PAPER
        )
        for trade_id in final:
            try:
                self._alert_exit(trade_id)
            except Exception:  # noqa: BLE001 - one exit never stops the others or the poll
                self.hook_errors += 1
                log.exception("desk: the exit alert of paper trade %s failed", trade_id)

    def _archived(self, alert_id: Any) -> dict | None:
        if not alert_id:
            return None
        return next((r for r in self.archive if r.get("id") == alert_id), None)

    def _mark_exit_told(self, sig: dict | None, what: str) -> None:
        """Record on the alert that its one exit has been told (kept for a restart)."""
        if sig is None:
            return
        sig["exit_told"] = what
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    update(desk_signals).where(desk_signals.c.id == sig["id"]).values(payload=sig)
                )
        except Exception:  # noqa: BLE001 - memory still holds it for this run
            log.exception("desk: could not store that alert %s was told its exit", sig["id"])

    def _alert_exit(self, trade_id: int) -> None:
        """The one exit of an alert (OF-5): its paper trade closed (or never filled). The
        archive row records it, the owner is told ONCE (EXIT), and a trade the owner closed
        early gets the alert's exit price beside theirs."""
        assert self.paper is not None
        trade = self.paper.trade(trade_id)
        if trade is None or trade.status not in FINAL_PAPER:
            return
        plan = trade.plan if isinstance(trade.plan, dict) else {}
        raw = plan.get("alert")
        snapshot: dict = raw if isinstance(raw, dict) else {}
        sig = self._archived(trade.alert_id)
        base: dict = sig if sig is not None else dict(snapshot)
        info = _exit_info(trade)
        self._note_slot_exit(base, info)
        told_before = bool(sig and sig.get("exit_told"))
        if sig is not None:  # in memory first: nothing below can make it tell twice
            sig["exit"] = info
            sig["exit_told"] = sig.get("exit_told") or info["reason"]
        taken = any(p.get("signal_id") == trade.alert_id for p in self.positions.values())
        if not told_before:  # one exit per alert, whichever path told it first
            self._send_alert_exit(trade, base, info, taken)
        # Bookkeeping after the message: a locked database here never costs the owner the
        # alert's one exit (the paper event comes once; QA cycle 2, BUG-14).
        if trade.alert_id in self.signals:
            # still pending when its exit came: the entry is over, it can no longer be taken
            self._guarded_write(
                "expire the pending card",
                lambda: self._resolve_signal(
                    trade.alert_id, "EXPIRED", "its exit alert came first"
                ),
            )
        if sig is not None:
            self._guarded_write("store the alert's exit", lambda: self._store_signal(sig))
        if info["price"] is not None:
            price = float(info["price"])
            self._guarded_write(
                "add the alert's exit to an early close",
                lambda: self._annotate_closed(trade.alert_id, price),
            )
        self.version += 1

    def _note_slot_exit(self, alert: dict, info: dict) -> None:
        """Remember when this strategy slot's trade closed: the Lab's cooldown counts from it.
        Only a trade that filled and CLOSED: a NO_FILL was never a position (the Lab's
        `last_exit_i` moves only when a position closes)."""
        t = self.trackers.get(str(alert.get("series_key") or ""))
        at = info.get("at")
        if t is None or not at or info.get("status") != "CLOSED":
            return
        when = _aware(dt.datetime.fromisoformat(str(at)))
        for slot in t.slots:
            if slot.cfg.name == alert.get("strategy") and slot.cfg.timeframe == alert.get(
                "timeframe"
            ):
                if slot.last_exit_at is None or (when and when > slot.last_exit_at):
                    slot.last_exit_at = when

    def _guarded_write(self, what: str, work: Any) -> None:
        try:
            work()
        except Exception:  # noqa: BLE001 - counted and logged; the poll carries on
            self.hook_errors += 1
            log.exception("desk: could not %s", what)

    def _store_signal(self, sig: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(desk_signals).where(desk_signals.c.id == sig["id"]).values(payload=sig)
            )

    def _send_alert_exit(self, trade: Any, base: dict, info: dict, taken: bool) -> None:
        label = str(base.get("label") or trade.series_key)
        side = str(base.get("side") or trade.side)
        raw_eco = base.get("economics")
        eco: dict = raw_eco if isinstance(raw_eco, dict) else {}
        raw_option = eco.get("option")
        option: dict = raw_option if isinstance(raw_option, dict) else {}
        words = info["reason"].replace("_", " ")
        price, net = info["price"], info["net"]
        at_price = f" @ ₹{price:,.2f}" if price is not None else ""
        alerted = str(base.get("created_at") or "")[11:16]
        if info["status"] == "NO_FILL":
            title = f"EXIT {label} — NO FILL" + (f" ({words})" if words != "NO FILL" else "")
            body = (
                f"The {alerted} alert never filled on paper, so it is over. If you entered "
                "anyway, close it at the market and report your exit price."
            )
        else:
            title = f"EXIT {label} — {words}{at_price}"
            body = f"The {alerted} {side.replace('_', ' ')} alert exits: {words}{at_price}." + (
                f" Paper net ₹{net:+,.0f}." if net is not None else ""
            )
        if taken and (info["status"] == "NO_FILL" or price is None):
            body += (
                " You took it: the desk closes your position at its last price; report your "
                "own exit to compare."
            )
        elif taken:
            body += (
                " You took it: the desk closes your position at the exit alert's price; "
                "report your own exit to compare."
            )
        position = {
            "id": f"alert-{trade.alert_id}",
            "label": label,
            "trading_symbol": base.get("trading_symbol") or trade.series_key,
            "series_key": trade.series_key,
            "kind": trade.kind,
            "side": side,
            "qty": trade.qty,
            "option": option,
            "unrealized": net,
        }
        self.notifier.send(
            Notification(
                title=title,
                body=body,
                level=NotificationLevel.EXIT,
                position_id=f"alert-{trade.alert_id}",
                payload={
                    # the phone prints this as the reason: a no-fill must say so
                    "advice": _exit_reason_line(info, words, at_price),
                    "position": position,
                    "exit": info,
                    "alert_id": trade.alert_id,
                },
            ),
            dedup_key=f"desk:alert-exit:{trade.alert_id}",
        )

    def _settle_taken(self, now: dt.datetime) -> None:
        """Close every taken position whose alert has exited (its paper trade is final) at
        the exit alert's price; a no-fill (no exit price) closes at the desk's price. This
        also ends positions left open before OF-5 whose alert exited long ago."""
        for pid, p in list(self.positions.items()):
            trade = self._paper_link(p)
            if trade is None or trade is UNREADABLE or trade.status not in FINAL_PAPER:
                continue
            price, source = _rupees(trade.exit_paise), "alert"
            if price is None:  # a no-fill (or an unpriced close): the desk's own mark
                price, source = self._mark_price(p, now), "desk"
            if price is None:
                p["hold_check"] = (
                    f"the alert has exited ({trade.close_reason or trade.status}) with no "
                    "price known here: close it with your own exit price"
                )
                continue
            when = _aware(trade.exit_ts or trade.updated_at) or now
            self._close(
                pid,
                reason=str(trade.close_reason or trade.status),
                source=source,
                alert_exit=price,
                now=max(when, _aware(dt.datetime.fromisoformat(p["opened_at"])) or when),
            )

    def _annotate_closed(self, alert_id: str, price: float) -> None:
        """A position the owner closed before the alert's exit gets that exit's price beside
        theirs, so the difference shows (OF-5)."""
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(desk_positions).where(
                        desk_positions.c.signal_id == alert_id,
                        desk_positions.c.status == "CLOSED",
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            p = dict(row["payload"] or {})
            if p.get("alert_exit_price") is not None:
                continue
            p.setdefault("id", row["id"])
            p["status"] = "CLOSED"
            p["alert_exit_price"] = price
            self._store_settled(row["id"], p, closed=True)

    def _paper_block(self) -> dict | None:
        """snapshot["paper"]: the book's status() and the desk's own hook_errors, apart."""
        if self.paper is None:
            return None
        try:
            status = dict(self.paper.status())
        except Exception:  # noqa: BLE001 - a broken status() cannot break the snapshot
            self.hook_errors += 1
            log.exception("paper: status() failed")
            status = {}
        return {**status, "desk_hook_errors": self.hook_errors}

    # ── snapshot ──────────────────────────────────────────────────────────
    def _rebuild_snapshot(self, now: dt.datetime, window) -> None:
        market_open = bool(window and window.is_open(now))
        served = {k: self._served_by(k) for k in self.trackers}
        delayed = {k for k, src in served.items() if is_delayed_source(src)}
        delayed_keys = sorted(delayed)  # snapshot output order; the set serves the lookups
        trackers = [
            t.as_dict(now, served[k], k in delayed, market_open) for k, t in self.trackers.items()
        ]
        pending = {s["instrument_key"]: s for s in self.signals.values()}
        open_by = {p["instrument_key"]: p for p in self.positions.values()}
        for d in trackers:
            if d["kind"] == "INDEX":
                self._add_reversal(d)  # US-068: "reversed k of 7, exit at 3"
            if self.delivery is not None and d["kind"] == "STOCK":
                d["pullback"] = self.delivery.card(d["key"])  # US-067 D11, D14
            d["signal"] = pending.get(d["tracked"])
            d["position"] = open_by.get(d["tracked"])
            d["state"] = (
                "HOLD"
                if d["position"]
                else "BUY"
                if (d["signal"] and d["signal"]["direction"] == "long")
                else "SELL"
                if d["signal"]
                else "TRACKING"
            )
            d["recent"] = [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "created_at",
                        "instrument_key",
                        "label",
                        "side",
                        "strategy",
                        "timeframe",
                        "status",
                        "strength",
                    )
                }
                | {
                    "outcome": (r.get("outcome") or {}).get("outcome"),
                    "r_now": (r.get("outcome") or {}).get("r_now"),
                }
                for r in self.archive
                if r["instrument_key"] == d["tracked"]
            ][:5]
        risk = self.governor.snapshot().as_dict()
        paper = self._paper_block()
        socket = None
        if self.source.primary is not None and hasattr(self.source.primary, "socket_alive"):
            socket = {
                "alive": self.source.primary.socket_alive(),
                "subscribed": getattr(self.source.primary, "subscribed", 0),
            }
        self.version += 1
        self._snapshot = {
            "updated_at": _iso(now),
            "booting": False,
            "version": self.version,
            "source": {
                "name": self.source.name,
                "delayed": bool(delayed_keys),  # header badge: any tracker on delayed bars
                "delayed_keys": delayed_keys,
                "resolution": self.source.resolution,
                "last_fetch_at": _iso(self.last_fetch_at),
                "last_error": self.last_error,
                "poll_seconds": self.cfg.poll_seconds,
                "polls": self.polls,
                "data_api_active": bool(self._data_api_active()),
                "socket": socket,
            },
            "session": {
                "trading_day": window is not None,
                "open": market_open,
                "entries_allowed": bool(window and window.entries_allowed(now)),
                "open_at": _iso(window.open_at) if window else None,
                "close_at": _iso(window.close_at) if window else None,
                "squareoff_at": window.mandatory_squareoff_at.strftime("%H:%M") if window else None,
                "holiday": (self.calendar.holiday_name(now.date()) if self.calendar else None),
            },
            "context": self._context_rows(),
            "positions": list(self.positions.values()),
            "signals": list(self.signals.values()),
            "indices": [d for d in trackers if d["kind"] == "INDEX"],
            "stocks": [d for d in trackers if d["kind"] == "STOCK"],
            "risk": risk,
            "paper": paper,
            # US-067: pending delivery alerts and carried delivery trades (None: no slot)
            "delivery": self.delivery.view() if self.delivery is not None else None,
            "scoreboard": self.scoreboard,
            "counts": {
                "instruments": len(trackers),
                "strategies": sum(len(t.slots) for t in self.trackers.values()),
                "pending": len(self.signals),
                "hold": len(self.positions),
                "signals_today": sum(
                    s.signals_today for t in self.trackers.values() for s in t.slots
                ),
                "archived": len(self.archive),
            },
            "config_path": self.cfg.path,
            "stock_horizon": self.cfg.stock_horizon,
        }

    def snapshot(self) -> dict:
        return self._snapshot
