"""The desk orchestrator.

One thread: pull bars for every watchlist series (one batched Yahoo call, or Dhan when
the Data API is active) -> update trackers -> evaluate every strategy slot on its own
closed bars -> raise BUY/SELL signals (one-unit economics, alert text) -> manage paper
positions the operator has acknowledged (trail, target, stop, square-off) -> cache a
snapshot the SSE stream ships every second.

The global RiskGovernor (daily loss, drawdown, consecutive losses, max open positions,
trades per day) is the master switch for every instrument, exactly as it is for the
Bank Nifty option engine. Nothing here can place an order.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from typing import Any

import pandas as pd
from sqlalchemy import Engine, select, update

from ..clock import IST, now_ist
from ..data.schema import desk_positions, desk_signals
from ..data.universe import load_specs
from ..notify.base import Notification, NotificationLevel
from .config import DeskConfig, load_desk_config
from .data import CompositeBars, build_source
from .economics import Economics, alert_text, index_option_economics, stock_economics
from .tracker import InstrumentTracker, StrategySlot, Verdict

log = logging.getLogger(__name__)


def _iso(v: dt.datetime | None) -> str | None:
    return v.isoformat() if v else None


def _aware(v: dt.datetime | None) -> dt.datetime | None:
    if v is None:
        return None
    return v.replace(tzinfo=dt.timezone.utc).astimezone(IST) if v.tzinfo is None else v.astimezone(IST)


class Desk:
    def __init__(self, engine: Engine, cfg_app, notifier, governor, session_clock, repo=None,
                 adapter_getter=None, data_api_active=None, desk_cfg: DeskConfig | None = None) -> None:
        self.engine = engine
        self.app_cfg = cfg_app
        self.notifier = notifier
        self.governor = governor
        self.session_clock = session_clock
        self.repo = repo
        self._adapter_getter = adapter_getter or (lambda: None)
        self._data_api_active = data_api_active or (lambda: False)
        self.cfg = desk_cfg or load_desk_config()
        self.series, self.instruments = load_specs()
        self.source: CompositeBars = build_source(self.cfg.source, self._adapter_getter(), bool(self._data_api_active()))
        self.fine_minutes = int(self.source.resolution[:-1])
        self.trackers: dict[str, InstrumentTracker] = {}
        self.context_bars: dict[str, pd.DataFrame] = {}
        self.context_manual: dict[str, dict] = {}
        self.signals: dict[str, dict] = {}        # pending, by id
        self.positions: dict[str, dict] = {}      # open, by id
        self.recent_signals: list[dict] = []
        self._snapshot: dict = {"updated_at": None, "indices": [], "stocks": [], "context": [], "positions": [],
                                "signals": [], "source": {"name": self.source.name}, "booting": True}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_fetch_at: dt.datetime | None = None
        self.last_error: str | None = None
        self.polls = 0
        self._build_trackers()
        self._recover()
        from .pulse import MarketPulse
        self.pulse = MarketPulse(engine, self, self._adapter_getter, self._data_api_active)

    # ── setup ─────────────────────────────────────────────────────────────
    def _build_trackers(self) -> None:
        self.trackers = {}
        for ic in self.cfg.instruments:
            s = self.series.get(ic.key)
            inst = self.instruments.get(ic.tracked)
            if s is None or inst is None:
                log.warning("desk: %s / %s not in universe.yaml; skipped", ic.key, ic.tracked)
                continue
            self.trackers[ic.key] = InstrumentTracker(ic, s, inst, self.fine_minutes)

    def reload(self) -> dict:
        with self._lock:
            self.cfg = load_desk_config()
            old = {k: t.bars for k, t in self.trackers.items()}
            self._build_trackers()
            for k, t in self.trackers.items():
                if k in old:
                    t.bars = old[k]
            self._wake.set()
        return {"instruments": len(self.trackers), "path": self.cfg.path}

    def _recover(self) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(select(desk_positions).where(desk_positions.c.status == "OPEN")).mappings().all()
            sigs = conn.execute(select(desk_signals).where(desk_signals.c.status == "PENDING")).mappings().all()
        for r in rows:
            p = dict(r["payload"] or {})
            p.update({"id": r["id"], "opened_at": _iso(_aware(r["opened_at"]))})
            self.positions[r["id"]] = p
        now = now_ist()
        for r in sigs:
            exp = _aware(r["expires_at"])
            if exp and exp < now:
                self._resolve_signal(r["id"], "EXPIRED")
        if self.positions:
            log.info("desk: recovered %d open position(s)", len(self.positions))

    # ── thread ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="desk-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception("desk poll failed")
            now = now_ist()
            window = self.session_clock.current_window(now)
            near = window is not None and (window.open_at - dt.timedelta(minutes=20)) <= now <= (window.close_at + dt.timedelta(minutes=5))
            wait = self.cfg.poll_seconds if near else 300
            self._wake.wait(wait)
            self._wake.clear()

    # ── one poll ──────────────────────────────────────────────────────────
    def poll(self) -> None:
        now = now_ist()
        self._maybe_switch_source()
        specs = {k: t.series for k, t in self.trackers.items()}
        for c in self.cfg.context:
            if not c.manual and c.key in self.series and c.key not in specs:
                specs[c.key] = self.series[c.key]
        since = {k: t.bars.index[-1] for k, t in self.trackers.items() if not t.bars.empty}
        got = self.source.fetch(specs, self.cfg.history_days, since)
        self.last_fetch_at = now
        self.last_error = self.source.last_error
        self.polls += 1
        with self._lock:
            for k, df in got.items():
                if k in self.trackers:
                    self.trackers[k].update_bars(df)
                else:
                    self.context_bars[k] = df
                if k in self.trackers and k in (c.key for c in self.cfg.context):
                    self.context_bars[k] = self.trackers[k].bars
            self._reset_daily_counters(now)
            window = self.session_clock.current_window(now)
            entries_ok = bool(window and window.entries_allowed(now))
            market_ok = bool(window and window.is_open(now))
            nifty = self.trackers.get("NIFTY")
            market_bars = nifty.bars if nifty is not None else self.context_bars.get("NIFTY")
            self._expire_signals(now)
            for t in self.trackers.values():
                fired = t.evaluate(now, market_bars, entries_ok and market_ok)
                for slot, v in fired:
                    self._raise_signal(t, slot, v, now)
            if market_ok or self.positions:
                self._manage_positions(now, window)
            self._rebuild_snapshot(now, window)
        try:
            self.pulse.maybe_record(now, market_ok)
        except Exception:  # noqa: BLE001
            log.exception("pulse failed")

    def _maybe_switch_source(self) -> None:
        want_dhan = self.cfg.source == "dhan" or (self.cfg.source == "auto" and bool(self._data_api_active()))
        has_dhan = self.source.primary is not None
        adapter = self._adapter_getter()
        if want_dhan and not has_dhan and adapter is not None:
            self.source = build_source("dhan", adapter, True)
            self.fine_minutes = int(self.source.resolution[:-1])
            for t in self.trackers.values():
                t.fine_minutes = self.fine_minutes
                t.bars = pd.DataFrame()
            log.info("desk: switched to Dhan 1-minute bars")
        elif has_dhan and (not want_dhan or adapter is None):
            self.source = build_source("yahoo", None, False)
            self.fine_minutes = int(self.source.resolution[:-1])
            for t in self.trackers.values():
                t.fine_minutes = self.fine_minutes
                t.bars = pd.DataFrame()
            log.info("desk: switched to Yahoo bars")

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

    # ── signals ───────────────────────────────────────────────────────────
    def _raise_signal(self, t: InstrumentTracker, slot: StrategySlot, v: Verdict, now: dt.datetime) -> None:
        if any(s["instrument_key"] == t.instrument.key for s in self.signals.values()):
            return                                              # one pending alert per instrument
        if any(p["instrument_key"] == t.instrument.key for p in self.positions.values()):
            return                                              # already holding it
        allowed, why = self.governor.may_open(now)
        per_symbol_cap = int(getattr(slot.strategy.p, "max_trades_per_day", 2) or 2)
        if t.trades_today >= per_symbol_cap:
            allowed, why = False, f"{t.cfg.key}: {t.trades_today} trade(s) today (cap {per_symbol_cap})"
        try:
            eco = self._economics(t, slot, v, now)
        except Exception as exc:  # noqa: BLE001
            log.exception("desk economics failed for %s", t.cfg.key)
            t.last_error = f"economics: {type(exc).__name__}: {exc}"
            return
        exit_time = str(getattr(slot.strategy.p, "exit_time", "15:15"))
        sid = uuid.uuid4().hex[:12]
        expires = now + dt.timedelta(minutes=self.cfg.signal_valid_minutes)
        text = alert_text(t.series.label, t.series.key, eco, slot.cfg.name, slot.cfg.timeframe, v.bar_ts, exit_time, v.reasons)
        sig = {
            "id": sid, "created_at": _iso(now), "expires_at": _iso(expires), "instrument_key": t.instrument.key,
            "series_key": t.series.key, "label": t.series.label, "kind": t.cfg.kind, "side": eco.side,
            "direction": v.direction, "strategy": slot.cfg.name, "strategy_label": slot.label,
            "timeframe": slot.cfg.timeframe, "bar_ts": _iso(v.bar_ts), "strength": v.strength,
            "reasons": v.reasons, "economics": eco.as_dict(), "text": text, "status": "PENDING",
            "risk_allowed": allowed, "risk_reason": why,
            "manage": {"target_r": float(getattr(slot.strategy.p, "target_r", 0) or 0),
                       "trail_atr_mult": float(getattr(slot.strategy.p, "trail_atr_mult", 0) or 0),
                       "trail_activate_r": float(getattr(slot.strategy.p, "trail_activate_r", 1.0) or 1.0),
                       "max_bars": int(getattr(slot.strategy.p, "max_bars", 0) or 0),
                       "exit_time": exit_time, "atr": (slot.last_features or {}).get("atr"),
                       "tf_min": slot.tf_min},
        }
        slot.signals_today += 1
        self.signals[sid] = sig
        self.recent_signals.insert(0, {k: sig[k] for k in ("id", "created_at", "instrument_key", "label", "side", "strategy",
                                                             "timeframe", "status", "strength")})
        self.recent_signals = self.recent_signals[:50]
        with self.engine.begin() as conn:
            conn.execute(desk_signals.insert().values(
                id=sid, created_at=now, expires_at=expires, instrument_key=t.instrument.key, series_key=t.series.key,
                kind=t.cfg.kind, side=eco.side, strategy=slot.cfg.name, timeframe=slot.cfg.timeframe,
                bar_ts=v.bar_ts, status="PENDING", payload=sig))
        title = f"{eco.side.replace('_', ' ')} {t.series.label}" + (f" {eco.option['strike']:g} {eco.option['opt_type']}" if eco.option else "") \
            + (f" · {eco.unit_label} @ ~₹{eco.entry_price:,.2f}" if eco.kind == "STOCK" else f" · ~₹{eco.outlay:,.0f} outlay")
        if not allowed:
            title = f"[BLOCKED by risk] {title}"
        self.notifier.send(Notification(title=title, body=text + ("" if allowed else f"\nRisk governor: {why}"),
                                        level=NotificationLevel.SIGNAL, signal_id=sid,
                                        ttl_seconds=self.cfg.signal_valid_minutes * 60, payload=sig),
                           dedup_key=f"desk:{sid}")

    def _economics(self, t: InstrumentTracker, slot: StrategySlot, v: Verdict, now: dt.datetime) -> Economics:
        target_r = float(getattr(slot.strategy.p, "target_r", 0) or 0)
        if t.cfg.kind == "STOCK":
            return stock_economics(t.instrument, v.direction, float(v.close), float(v.stop), target_r)
        vix = self._context_last("INDIAVIX")
        return index_option_economics(t.instrument, v.direction, float(v.close), float(v.stop), target_r, now, vix)

    def _expire_signals(self, now: dt.datetime) -> None:
        for sid, s in list(self.signals.items()):
            if dt.datetime.fromisoformat(s["expires_at"]) <= now:
                self._resolve_signal(sid, "EXPIRED")

    def _resolve_signal(self, sid: str, status: str, note: str | None = None) -> None:
        s = self.signals.pop(sid, None)
        with self.engine.begin() as conn:
            conn.execute(update(desk_signals).where(desk_signals.c.id == sid)
                         .values(status=status, resolved_at=now_ist()))
        for r in self.recent_signals:
            if r["id"] == sid:
                r["status"] = status
        if s and self.repo is not None:
            try:
                self.repo.event(now_ist(), "INFO", "desk_signal", f"{sid} {status}", {"note": note})
            except Exception:  # noqa: BLE001
                pass

    def acknowledge(self, sid: str, action: str, fill_price: float | None = None, source: str = "dashboard") -> dict:
        with self._lock:
            s = self.signals.get(sid)
            if s is None:
                return {"accepted": False, "message": "signal not pending (expired or already resolved)"}
            act = action.upper()
            if act in ("SKIP", "SKIPPED"):
                self._resolve_signal(sid, "SKIPPED")
                return {"accepted": True, "message": "skipped"}
            if act not in ("TAKE", "TAKEN"):
                return {"accepted": False, "message": "action must be TAKE or SKIP"}
            now = now_ist()
            eco = s["economics"]
            t = self.trackers.get(s["series_key"])
            entry = float(fill_price) if fill_price else float(eco["entry_price"])
            pid = uuid.uuid4().hex[:12]
            # the stop travels with the fill so the rupee risk stays what was alerted
            pos = {
                "id": pid, "signal_id": sid, "instrument_key": s["instrument_key"], "series_key": s["series_key"],
                "label": s["label"], "kind": s["kind"], "side": s["side"], "direction": s["direction"],
                "strategy": s["strategy"], "timeframe": s["timeframe"], "opened_at": _iso(now), "qty": int(eco["qty"]),
                "entry_price": entry, "entry_underlying": float(eco["underlying_entry"]),
                "stop_underlying": float(eco["underlying_stop"]), "target_underlying": eco.get("underlying_target"),
                "initial_stop_underlying": float(eco["underlying_stop"]), "option": eco.get("option") or {},
                "manage": s["manage"], "trail_active": False, "bars_held": 0, "last_underlying": float(eco["underlying_entry"]),
                "unrealized": 0.0, "r_now": 0.0, "exit_advice": None, "status": "OPEN", "source": source,
                "notes": list(eco.get("notes") or []),
            }
            self.positions[pid] = pos
            if t is not None:
                t.trades_today += 1
            self.governor.record_open(now)
            with self.engine.begin() as conn:
                conn.execute(desk_positions.insert().values(
                    id=pid, signal_id=sid, instrument_key=pos["instrument_key"], series_key=pos["series_key"],
                    kind=pos["kind"], side=pos["side"], strategy=pos["strategy"], timeframe=pos["timeframe"],
                    opened_at=now, status="OPEN", entry_price=entry, qty=pos["qty"], payload=pos))
            self._resolve_signal(sid, "TAKEN")
            self.notifier.info(f"Position opened: {s['label']} {s['side'].replace('_', ' ')}",
                               f"entry {entry:,.2f} · stop {pos['stop_underlying']:,.2f} (underlying) · managed every poll",
                               dedup_key=f"desk:open:{pid}")
            return {"accepted": True, "message": "position opened; the desk now manages it", "position_id": pid}

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
            risk_pts = abs(p["entry_underlying"] - p["initial_stop_underlying"]) or 1e-9
            r_now = sign * (last - p["entry_underlying"]) / risk_pts
            p["last_underlying"], p["r_now"] = last, round(r_now, 3)
            p["unrealized"] = round(self._unrealized(p, last, now), 2)
            # trail
            if m.get("trail_atr_mult") and r_now >= float(m.get("trail_activate_r", 1.0)):
                atr = m.get("atr") or t.regime.get("atr") or 0.0
                if atr:
                    new_stop = last - sign * float(m["trail_atr_mult"]) * float(atr)
                    if (sign > 0 and new_stop > p["stop_underlying"]) or (sign < 0 and new_stop < p["stop_underlying"]):
                        p["stop_underlying"], p["trail_active"] = round(new_stop, 2), True
            # bars held
            opened = dt.datetime.fromisoformat(p["opened_at"])
            p["bars_held"] = int(max(0, (now - opened).total_seconds() // 60) // max(1, int(m.get("tf_min", 5))))
            advice = None
            if (sign > 0 and last <= p["stop_underlying"]) or (sign < 0 and last >= p["stop_underlying"]):
                advice = "STOP" if not p["trail_active"] else "TRAIL_STOP"
            elif p.get("target_underlying") and ((sign > 0 and last >= p["target_underlying"]) or (sign < 0 and last <= p["target_underlying"])):
                advice = "TARGET"
            elif window is not None:
                hh, mm = (int(x) for x in str(m.get("exit_time", "15:15")).split(":"))
                if now.time() >= dt.time(hh, mm):
                    advice = "SQUARE_OFF"
            if advice is None and m.get("max_bars") and p["bars_held"] >= int(m["max_bars"]):
                advice = "TIME_STOP"
            p["exit_advice"] = advice
            if advice:
                self.notifier.send(Notification(
                    title=f"EXIT {p['label']} — {advice.replace('_', ' ')}",
                    body=(f"{p['side'].replace('_', ' ')} from {p['entry_price']:,.2f}; underlying now {last:,.2f} "
                          f"({r_now:+.2f}R, est. {p['unrealized']:+,.0f}). Close it at the market and record the exit price."),
                    level=NotificationLevel.EXIT, position_id=pid, payload={"advice": advice, "position": p}),
                    dedup_key=f"desk:exit:{pid}:{advice}")
            with self.engine.begin() as conn:
                conn.execute(update(desk_positions).where(desk_positions.c.id == pid).values(payload=p))

    def _unrealized(self, p: dict, last: float, now: dt.datetime) -> float:
        sign = 1 if p["direction"] == "long" else -1
        if p["kind"] == "STOCK":
            return sign * (last - p["entry_price"]) * p["qty"]
        o = p.get("option") or {}
        if not o:
            return 0.0
        from ..lab.execution import OptionPricer, expiry_datetime

        vix = self._context_last("INDIAVIX")
        ts = pd.Timestamp(now).tz_convert(IST)
        pricer = OptionPricer(self.app_cfg.backtest,
                              pd.Series([vix], index=[ts.normalize()]) if vix else None)
        prem = pricer.premium(last, float(o["strike"]), ts, expiry_datetime(dt.date.fromisoformat(o["expiry"])), o["opt_type"])
        return (prem - p["entry_price"]) * p["qty"]

    def close_position(self, pid: str, exit_price: float, reason: str = "MANUAL") -> dict | None:
        with self._lock:
            p = self.positions.pop(pid, None)
            if p is None:
                return None
            now = now_ist()
            t = self.trackers.get(p["series_key"])
            inst = self.instruments.get(p["instrument_key"])
            sign = 1 if p["direction"] == "long" else -1
            qty = int(p["qty"])
            gross = sign * (float(exit_price) - float(p["entry_price"])) * qty if p["kind"] == "STOCK" \
                else (float(exit_price) - float(p["entry_price"])) * qty
            costs = 0.0
            if inst is not None:
                from .economics import _costs_for
                try:
                    costs = _costs_for(inst).round_trip_cost(float(p["entry_price"]), float(exit_price), qty)
                except Exception:  # noqa: BLE001
                    costs = 0.0
            net = gross - costs
            p.update({"status": "CLOSED", "closed_at": _iso(now), "exit_price": float(exit_price), "gross_pnl": round(gross, 2),
                      "costs": round(costs, 2), "net_pnl": round(net, 2), "exit_reason": reason})
            with self.engine.begin() as conn:
                conn.execute(update(desk_positions).where(desk_positions.c.id == pid).values(
                    status="CLOSED", closed_at=now, exit_price=float(exit_price), gross_pnl=gross, costs=costs,
                    net_pnl=net, exit_reason=reason, payload=p))
            self.governor.record_close(net, now)
            if t is not None:
                t.pnl_today += net
                t.consecutive_losses = t.consecutive_losses + 1 if net < 0 else 0
            self.notifier.info(f"Closed {p['label']}: {net:+,.0f} net", f"{reason} at {exit_price:,.2f}; costs {costs:,.0f}",
                               dedup_key=f"desk:close:{pid}")
            return {"closed": True, "net_pnl": round(net, 2), "gross_pnl": round(gross, 2), "costs": round(costs, 2)}

    def journal(self, limit: int = 200) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(desk_positions).where(desk_positions.c.status == "CLOSED")
                                .order_by(desk_positions.c.closed_at.desc()).limit(limit)).mappings().all()
        out = []
        for r in rows:
            d = {k: r[k] for k in ("id", "instrument_key", "series_key", "kind", "side", "strategy", "timeframe",
                                   "entry_price", "exit_price", "qty", "gross_pnl", "costs", "net_pnl", "exit_reason")}
            d["opened_at"], d["closed_at"] = _iso(_aware(r["opened_at"])), _iso(_aware(r["closed_at"]))
            out.append(d)
        return out

    # ── context strip ─────────────────────────────────────────────────────
    def set_context(self, key: str, last: float, change_pct: float | None = None, note: str | None = None) -> dict:
        key = key.upper()
        self.context_manual[key] = {"last": float(last), "change_pct": change_pct, "note": note, "as_of": _iso(now_ist())}
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
        for c in self.cfg.context:
            row: dict[str, Any] = {"key": c.key, "label": c.label, "manual": c.manual, "note": c.note, "last": None,
                                   "change_pct": None, "prev_close": None, "as_of": None, "source": None}
            df = self.context_bars.get(c.key)
            if df is None and c.key in self.trackers:
                df = self.trackers[c.key].bars
            if df is not None and not df.empty:
                day = df.index.normalize()
                closes = df["close"].groupby(day).last()
                row["last"] = float(closes.iloc[-1])
                if len(closes) >= 2:
                    row["prev_close"] = float(closes.iloc[-2])
                    row["change_pct"] = (row["last"] / row["prev_close"] - 1) * 100
                row["as_of"] = _iso(df.index[-1].to_pydatetime())
                row["source"] = self.source.served_by.get(c.key, self.source.name)
            m = self.context_manual.get(c.key)
            if m:
                row.update({"last": m["last"], "change_pct": m.get("change_pct"), "as_of": m.get("as_of"),
                            "source": "manual", "note": m.get("note") or c.note})
            rows.append(row)
        return rows

    # ── snapshot ──────────────────────────────────────────────────────────
    def _rebuild_snapshot(self, now: dt.datetime, window) -> None:
        delayed = bool(self.source.delayed)
        market_open = bool(window and window.is_open(now))
        trackers = [t.as_dict(now, self.source.served_by.get(t.cfg.key, self.source.name), delayed, market_open)
                    for t in self.trackers.values()]
        pending = {s["instrument_key"]: s for s in self.signals.values()}
        open_by = {p["instrument_key"]: p for p in self.positions.values()}
        halted = self.governor.state.value == "HALTED"
        for d in trackers:
            d["signal"] = pending.get(d["tracked"])
            d["position"] = open_by.get(d["tracked"])
            d["state"] = ("HOLD" if d["position"] else "BUY" if (d["signal"] and d["signal"]["direction"] == "long")
                          else "SELL" if d["signal"] else ("HALTED" if halted else "TRACKING"))
            d["recent"] = [r for r in self.recent_signals if r["instrument_key"] == d["tracked"]][:5]
        risk = self.governor.snapshot().as_dict()
        self._snapshot = {
            "updated_at": _iso(now), "booting": False,
            "source": {"name": self.source.name, "delayed": delayed, "resolution": self.source.resolution,
                       "last_fetch_at": _iso(self.last_fetch_at), "last_error": self.last_error,
                       "poll_seconds": self.cfg.poll_seconds, "polls": self.polls,
                       "data_api_active": bool(self._data_api_active())},
            "session": {"trading_day": window is not None, "open": bool(window and window.is_open(now)),
                        "entries_allowed": bool(window and window.entries_allowed(now)),
                        "open_at": _iso(window.open_at) if window else None,
                        "close_at": _iso(window.close_at) if window else None,
                        "squareoff_at": window.mandatory_squareoff_at.strftime("%H:%M") if window else None},
            "context": self._context_rows(),
            "positions": list(self.positions.values()),
            "signals": list(self.signals.values()),
            "indices": [d for d in trackers if d["kind"] == "INDEX"],
            "stocks": [d for d in trackers if d["kind"] == "STOCK"],
            "risk": risk,
            "counts": {"instruments": len(trackers), "strategies": sum(len(t.slots) for t in self.trackers.values()),
                       "pending": len(self.signals), "hold": len(self.positions),
                       "signals_today": sum(s.signals_today for t in self.trackers.values() for s in t.slots)},
            "config_path": self.cfg.path,
        }

    def snapshot(self) -> dict:
        return self._snapshot
