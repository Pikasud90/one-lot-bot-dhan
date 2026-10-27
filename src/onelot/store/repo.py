"""Repository — the only place SQL is written.

Keeps the engine free of query construction and gives every read/write a
single, testable place to enforce ordering, tz handling and idempotency.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from sqlalchemy import and_, desc, func, insert, select, update

from ..clock import to_ist
from .db import Database
from .schema import (
    atm_iv_history,
    candles,
    chain_snapshots,
    daily_bars,
    engine_events,
    gate_audit,
    instruments,
    positions,
    regime_history,
    risk_state,
    signal_evaluations,
    signals,
    trade_journal,
)

log = logging.getLogger(__name__)


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ── candles ───────────────────────────────────────────────────────────
    def upsert_candle(self, instrument_key: str, interval_min: int, ts: dt.datetime,
                      o: float, h: float, low: float, c: float,
                      volume: float | None, closed: bool = True,
                      source: str = "live") -> None:
        self.db.upsert(candles, {
            "instrument_key": instrument_key, "interval_min": interval_min, "ts": ts,
            "open": o, "high": h, "low": low, "close": c,
            # NULL not 0 — an index genuinely has no volume (defect B3).
            "volume": volume, "closed": closed, "source": source,
        }, ["instrument_key", "interval_min", "ts"])

    def bulk_upsert_candles(self, rows: list[dict]) -> int:
        for r in rows:
            self.upsert_candle(**r)
        return len(rows)

    def get_candles(self, instrument_key: str, interval_min: int,
                    limit: int = 500, closed_only: bool = True) -> list[dict]:
        """Most recent `limit` bars, returned OLDEST FIRST for indicator input.

        `closed_only` defaults to True: INVARIANT I2 means the strategy never
        sees a forming bar (v1 defect C4 — repainting).
        """
        cond = and_(candles.c.instrument_key == instrument_key,
                    candles.c.interval_min == interval_min)
        if closed_only:
            cond = and_(cond, candles.c.closed.is_(True))
        stmt = select(candles).where(cond).order_by(desc(candles.c.ts)).limit(limit)
        rows = self.db.fetch_all(stmt)
        rows.reverse()
        for r in rows:
            r["ts"] = to_ist(r["ts"])
        return rows

    def last_candle_ts(self, instrument_key: str, interval_min: int) -> dt.datetime | None:
        row = self.db.fetch_one(
            select(func.max(candles.c.ts).label("m")).where(
                and_(candles.c.instrument_key == instrument_key,
                     candles.c.interval_min == interval_min)))
        return to_ist(row["m"]) if row and row["m"] else None

    # ── daily bars (HMM training) ─────────────────────────────────────────
    def upsert_daily_bar(self, symbol: str, trade_date: str, o: float, h: float,
                         low: float, c: float, volume: float | None) -> None:
        self.db.upsert(daily_bars, {
            "symbol": symbol, "trade_date": trade_date,
            "open": o, "high": h, "low": low, "close": c, "volume": volume,
        }, ["symbol", "trade_date"])

    def get_daily_bars(self, symbol: str, limit: int = 750) -> list[dict]:
        stmt = (select(daily_bars).where(daily_bars.c.symbol == symbol)
                .order_by(desc(daily_bars.c.trade_date)).limit(limit))
        rows = self.db.fetch_all(stmt)
        rows.reverse()
        return rows

    # ── option chain ──────────────────────────────────────────────────────
    def record_chain_snapshot(self, ts: dt.datetime, underlying: str, expiry: str,
                              rows: list[dict]) -> int:
        payload = [{
            "ts": ts, "underlying": underlying, "expiry": expiry,
            "strike": r.get("strike"), "opt_type": r.get("opt_type"),
            "security_id": str(r.get("security_id") or ""),
            "ltp": r.get("ltp"), "bid": r.get("bid"), "ask": r.get("ask"),
            "bid_qty": r.get("bid_qty"), "ask_qty": r.get("ask_qty"),
            "volume": r.get("volume"), "oi": r.get("oi"), "iv": r.get("iv"),
            "delta": r.get("delta"), "gamma": r.get("gamma"),
            "theta": r.get("theta"), "vega": r.get("vega"),
            "greek_source": r.get("greek_source"),
        } for r in rows]
        if not payload:
            return 0
        with self.db.begin() as conn:
            conn.execute(insert(chain_snapshots), payload)
        return len(payload)

    # ── ATM IV history (IV percentile) ────────────────────────────────────
    def record_atm_iv(self, trade_date: str, underlying: str, atm_iv: float,
                      atm_strike: float | None, underlying_price: float | None,
                      ts: dt.datetime) -> None:
        self.db.upsert(atm_iv_history, {
            "trade_date": trade_date, "underlying": underlying, "atm_iv": atm_iv,
            "atm_strike": atm_strike, "underlying_price": underlying_price,
            "n_samples": 1, "recorded_at": ts,
        }, ["trade_date", "underlying"])

    def atm_iv_series(self, underlying: str, limit: int = 252,
                      before_date: str | None = None) -> list[float]:
        """ATM IV history, oldest first.

        `before_date` excludes the current session, which is what keeps the IV
        percentile from ranking today's IV against itself.
        """
        cond = atm_iv_history.c.underlying == underlying
        if before_date:
            cond = and_(cond, atm_iv_history.c.trade_date < before_date)
        stmt = (select(atm_iv_history.c.atm_iv, atm_iv_history.c.trade_date)
                .where(cond).order_by(desc(atm_iv_history.c.trade_date)).limit(limit))
        rows = self.db.fetch_all(stmt)
        rows.reverse()
        return [float(r["atm_iv"]) for r in rows if r["atm_iv"] is not None]

    # ── evaluations and gate audit ────────────────────────────────────────
    def record_evaluation(self, eval_id: str, ts: dt.datetime, bar_ts: dt.datetime,
                          direction: str | None, emitted: bool,
                          blocked_by: str | None, payload: dict) -> None:
        with self.db.begin() as conn:
            conn.execute(insert(signal_evaluations).values(
                eval_id=eval_id, ts=ts, bar_ts=bar_ts, direction=direction,
                emitted=emitted, blocked_by=blocked_by,
                payload=json.loads(json.dumps(payload, default=str)),
            ))

    def record_gates(self, eval_id: str, ts: dt.datetime, direction: str | None,
                     results: list[dict]) -> int:
        if not results:
            return 0
        rows = [{
            "eval_id": eval_id, "ts": ts, "gate": r["gate"], "direction": direction,
            "passed": bool(r["passed"]), "value": str(r.get("value", "")),
            "reason": str(r.get("reason", "")),
        } for r in results]
        with self.db.begin() as conn:
            conn.execute(insert(gate_audit), rows)
        return len(rows)

    def gate_rejection_histogram(self, since: dt.datetime) -> list[dict]:
        """Answers 'why did I get no signal today?' — v1 could not (defect B2)."""
        stmt = (select(gate_audit.c.gate,
                       func.count().label("evaluations"),
                       func.sum(func.cast(gate_audit.c.passed, __import__("sqlalchemy").Integer))
                       .label("passes"))
                .where(gate_audit.c.ts >= since)
                .group_by(gate_audit.c.gate))
        rows = self.db.fetch_all(stmt)
        out = []
        for r in rows:
            ev = int(r["evaluations"] or 0)
            pa = int(r["passes"] or 0)
            out.append({"gate": r["gate"], "evaluations": ev, "passes": pa,
                        "rejections": ev - pa,
                        "pass_rate": round(pa / ev, 3) if ev else 0.0})
        return sorted(out, key=lambda d: -d["rejections"])

    # ── signals ───────────────────────────────────────────────────────────
    def insert_signal(self, values: dict) -> None:
        v = dict(values)
        if isinstance(v.get("payload"), dict):
            v["payload"] = json.loads(json.dumps(v["payload"], default=str))
        with self.db.begin() as conn:
            conn.execute(insert(signals).values(**v))

    def get_signal(self, signal_id: str) -> dict | None:
        return self.db.fetch_one(select(signals).where(signals.c.signal_id == signal_id))

    def pending_signals(self) -> list[dict]:
        return self.db.fetch_all(
            select(signals).where(signals.c.status == "PENDING")
            .order_by(desc(signals.c.created_at)))

    def resolve_signal(self, signal_id: str, status: str, source: str,
                       ts: dt.datetime) -> bool:
        """Idempotent acknowledgement.

        v1 defect D7: the dashboard and Telegram were two sources of truth for
        the most safety-critical action in the system. Here both clients hit
        this one function, and a second ack is a no-op rather than a second trade.
        """
        with self.db.begin() as conn:
            res = conn.execute(
                update(signals)
                .where(and_(signals.c.signal_id == signal_id,
                            signals.c.status == "PENDING"))
                .values(status=status, resolved_at=ts, resolution_source=source))
        return (res.rowcount or 0) > 0

    def expire_stale_signals(self, now: dt.datetime) -> int:
        with self.db.begin() as conn:
            res = conn.execute(
                update(signals)
                .where(and_(signals.c.status == "PENDING", signals.c.expires_at < now))
                .values(status="EXPIRED", resolved_at=now, resolution_source="ttl"))
        return res.rowcount or 0

    def recent_signals(self, since: dt.datetime) -> list[dict]:
        return self.db.fetch_all(
            select(signals).where(signals.c.created_at >= since)
            .order_by(desc(signals.c.created_at)))

    # ── positions ─────────────────────────────────────────────────────────
    def insert_position(self, values: dict) -> None:
        v = dict(values)
        if isinstance(v.get("payload"), dict):
            v["payload"] = json.loads(json.dumps(v["payload"], default=str))
        with self.db.begin() as conn:
            conn.execute(insert(positions).values(**v))

    def update_position(self, position_id: str, **values) -> None:
        with self.db.begin() as conn:
            conn.execute(update(positions)
                         .where(positions.c.position_id == position_id).values(**values))

    def get_position(self, position_id: str) -> dict | None:
        return self.db.fetch_one(
            select(positions).where(positions.c.position_id == position_id))

    def open_positions(self, mode: str | None = None) -> list[dict]:
        """Used at boot to resume the Guardian after a crash (v1 defect D5)."""
        cond = positions.c.state.in_(("POSITION_OPEN", "EXIT_PENDING"))
        if mode:
            cond = and_(cond, positions.c.mode == mode)
        return self.db.fetch_all(select(positions).where(cond)
                                 .order_by(desc(positions.c.opened_at)))

    # ── journal ───────────────────────────────────────────────────────────
    def insert_journal(self, values: dict) -> None:
        v = dict(values)
        if isinstance(v.get("cost_breakdown"), dict):
            v["cost_breakdown"] = json.loads(json.dumps(v["cost_breakdown"], default=str))
        with self.db.begin() as conn:
            conn.execute(insert(trade_journal).values(**v))

    def journal_for_date(self, trade_date: str, mode: str | None = None) -> list[dict]:
        cond = trade_journal.c.trade_date == trade_date
        if mode:
            cond = and_(cond, trade_journal.c.mode == mode)
        return self.db.fetch_all(select(trade_journal).where(cond)
                                 .order_by(trade_journal.c.closed_at))

    def all_journal(self, mode: str | None = None, limit: int = 5000) -> list[dict]:
        stmt = select(trade_journal)
        if mode:
            stmt = stmt.where(trade_journal.c.mode == mode)
        return self.db.fetch_all(stmt.order_by(desc(trade_journal.c.closed_at)).limit(limit))

    # ── risk state ────────────────────────────────────────────────────────
    def get_risk_state(self, trade_date: str, mode: str) -> dict | None:
        return self.db.fetch_one(select(risk_state).where(
            and_(risk_state.c.trade_date == trade_date, risk_state.c.mode == mode)))

    def save_risk_state(self, values: dict) -> None:
        self.db.upsert(risk_state, values, ["trade_date", "mode"])

    # ── regime ────────────────────────────────────────────────────────────
    def save_regime(self, trade_date: str, symbol: str, state: str, confidence: float,
                    model_hash: str | None, features: dict, ts: dt.datetime) -> None:
        self.db.upsert(regime_history, {
            "trade_date": trade_date, "symbol": symbol, "state": state,
            "confidence": confidence, "model_hash": model_hash,
            "features": json.loads(json.dumps(features, default=str)),
            "computed_at": ts,
        }, ["trade_date", "symbol"])

    def get_regime(self, trade_date: str, symbol: str) -> dict | None:
        return self.db.fetch_one(select(regime_history).where(
            and_(regime_history.c.trade_date == trade_date,
                 regime_history.c.symbol == symbol)))

    # ── instruments ───────────────────────────────────────────────────────
    def upsert_instruments(self, rows: list[dict]) -> int:
        for r in rows:
            self.db.upsert(instruments, r, ["security_id", "exchange_segment"])
        return len(rows)

    def find_contract(self, underlying: str, expiry: str, strike: float,
                      opt_type: str) -> dict | None:
        return self.db.fetch_one(select(instruments).where(and_(
            instruments.c.underlying == underlying, instruments.c.expiry == expiry,
            instruments.c.strike == strike, instruments.c.opt_type == opt_type)))

    def lot_size_for(self, underlying: str, expiry: str) -> int | None:
        row = self.db.fetch_one(select(instruments.c.lot_size).where(and_(
            instruments.c.underlying == underlying,
            instruments.c.expiry == expiry)).limit(1))
        return int(row["lot_size"]) if row and row["lot_size"] else None

    # ── events ────────────────────────────────────────────────────────────
    def event(self, ts: dt.datetime, level: str, kind: str, message: str,
              payload: dict[str, Any] | None = None) -> None:
        with self.db.begin() as conn:
            conn.execute(insert(engine_events).values(
                ts=ts, level=level, kind=kind, message=message,
                payload=json.loads(json.dumps(payload or {}, default=str))))

    def recent_events(self, limit: int = 200) -> list[dict]:
        return self.db.fetch_all(
            select(engine_events).order_by(desc(engine_events.c.ts)).limit(limit))

    # ── retention ─────────────────────────────────────────────────────────
    def apply_retention(self, quote_days: int, chain_days: int) -> dict[str, int]:
        return {
            "chain_snapshots": self.db.purge_older_than(chain_snapshots, "ts", chain_days),
            "engine_events": self.db.purge_older_than(engine_events, "ts", quote_days),
        }
