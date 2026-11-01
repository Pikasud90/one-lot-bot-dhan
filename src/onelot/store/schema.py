"""Database schema, backend-agnostic SQLAlchemy Core.

Everything the engine decides is persisted: not just prices, but every gate
evaluation and every state transition. That is what makes "why did I get no
signal on Tuesday?" an answerable question (v1 had no answer) and what lets
the replay harness reproduce a live session exactly.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)

UTC = dt.timezone.utc
IST = ZoneInfo("Asia/Kolkata")


class TZDateTime(TypeDecorator):
    """Timezone-safe datetime that behaves identically on SQLite and Postgres.

    SQLite has no native timezone type and silently DROPS tzinfo, so a naive
    read of a `TZDateTime` column comes back as a bare datetime
    that then gets interpreted as local time. On a machine that is not in IST
    that shifts every stored bar (v1 defect C5).

    We normalise to UTC on write and re-attach UTC on read; callers convert to
    IST at the edge via `onelot.clock.to_ist`.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


METADATA = MetaData()

# ── market data ───────────────────────────────────────────────────────────

candles = Table(
    "candles",
    METADATA,
    Column("instrument_key", String(64), primary_key=True),
    Column("interval_min", Integer, primary_key=True),
    Column("ts", TZDateTime, primary_key=True),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    # NULL, not 0, when the series genuinely has no volume (the index).
    # Storing 0 is what let v1 compute VWAP on 0/0 (defect B3).
    Column("volume", Float, nullable=True),
    Column("closed", Boolean, nullable=False, default=True),
    Column("source", String(16), nullable=False, default="live"),
    Index("ix_candles_ts", "ts"),
)

chain_snapshots = Table(
    "chain_snapshots",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", TZDateTime, nullable=False),
    Column("underlying", String(32), nullable=False),
    Column("expiry", String(16), nullable=False),
    Column("strike", Float, nullable=False),
    Column("opt_type", String(2), nullable=False),
    Column("security_id", String(32)),
    Column("ltp", Float),
    Column("bid", Float),
    Column("ask", Float),
    Column("bid_qty", Integer),
    Column("ask_qty", Integer),
    Column("volume", Float),
    Column("oi", Float),
    Column("iv", Float),
    Column("delta", Float),
    Column("gamma", Float),
    Column("theta", Float),
    Column("vega", Float),
    Column("greek_source", String(16)),  # broker | local | cross_checked
    Index("ix_chain_ts", "ts"),
    Index("ix_chain_lookup", "underlying", "expiry", "strike", "opt_type", "ts"),
)

atm_iv_history = Table(
    "atm_iv_history",
    METADATA,
    Column("trade_date", String(10), primary_key=True),
    Column("underlying", String(32), primary_key=True),
    Column("atm_iv", Float, nullable=False),
    Column("atm_strike", Float),
    Column("underlying_price", Float),
    Column("n_samples", Integer, default=1),
    Column("recorded_at", TZDateTime),
)

daily_bars = Table(
    "daily_bars",
    METADATA,
    Column("symbol", String(32), primary_key=True),
    Column("trade_date", String(10), primary_key=True),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("volume", Float),
)

# ── decisions ─────────────────────────────────────────────────────────────

signal_evaluations = Table(
    "signal_evaluations",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("eval_id", String(40), nullable=False, unique=True),
    Column("ts", TZDateTime, nullable=False),
    Column("bar_ts", TZDateTime, nullable=False),
    Column("direction", String(2)),
    Column("emitted", Boolean, nullable=False, default=False),
    Column("blocked_by", String(48)),
    Column("payload", JSON),
    Index("ix_eval_ts", "ts"),
)

gate_audit = Table(
    "gate_audit",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("eval_id", String(40), nullable=False),
    Column("ts", TZDateTime, nullable=False),
    Column("gate", String(48), nullable=False),
    Column("direction", String(2)),
    Column("passed", Boolean, nullable=False),
    Column("value", Text),
    Column("reason", Text),
    Index("ix_gate_eval", "eval_id"),
    Index("ix_gate_name_ts", "gate", "ts"),
)

signals = Table(
    "signals",
    METADATA,
    Column("signal_id", String(40), primary_key=True),
    Column("eval_id", String(40)),
    Column("created_at", TZDateTime, nullable=False),
    Column("expires_at", TZDateTime, nullable=False),
    Column("direction", String(2), nullable=False),
    Column("underlying", String(32), nullable=False),
    Column("expiry", String(16), nullable=False),
    Column("strike", Float, nullable=False),
    Column("opt_type", String(2), nullable=False),
    Column("security_id", String(32)),
    Column("quote_bid", Float),
    Column("quote_ask", Float),
    Column("quote_ltp", Float),
    Column("entry_eff", Float),
    Column("stop_level", Float),
    Column("target_level", Float),
    Column("breakeven", Float),
    Column("lots", Integer),
    Column("qty", Integer),
    Column("lot_size", Integer),
    Column("net_risk", Float),
    Column("net_reward", Float),
    Column("rr_net", Float),
    Column("delta", Float),
    Column("iv", Float),
    Column("theta_per_hour", Float),
    Column("status", String(24), nullable=False, default="PENDING"),
    Column("resolved_at", TZDateTime),
    Column("resolution_source", String(24)),
    Column("payload", JSON),
    Index("ix_signals_created", "created_at"),
    Index("ix_signals_status", "status"),
)

# ── positions and results ─────────────────────────────────────────────────

positions = Table(
    "positions",
    METADATA,
    Column("position_id", String(40), primary_key=True),
    Column("signal_id", String(40)),
    Column("state", String(24), nullable=False),
    Column("opened_at", TZDateTime, nullable=False),
    Column("closed_at", TZDateTime),
    Column("direction", String(2), nullable=False),
    Column("underlying", String(32)),
    Column("expiry", String(16)),
    Column("strike", Float),
    Column("opt_type", String(2)),
    Column("security_id", String(32)),
    Column("lots", Integer),
    Column("qty", Integer),
    Column("lot_size", Integer),
    Column("entry_price", Float, nullable=False),
    Column("initial_stop", Float, nullable=False),
    Column("target", Float),
    Column("trail_stop", Float),
    Column("trail_active", Boolean, default=False),
    Column("high_water_premium", Float),
    Column("r_per_unit", Float),
    Column("exit_price", Float),
    Column("exit_reason", String(32)),
    Column("degraded_data", Boolean, default=False),
    Column("mode", String(8), nullable=False, default="PAPER"),
    Column("payload", JSON),
    Index("ix_pos_state", "state"),
)

trade_journal = Table(
    "trade_journal",
    METADATA,
    Column("trade_id", String(40), primary_key=True),
    Column("position_id", String(40)),
    Column("trade_date", String(10), nullable=False),
    Column("closed_at", TZDateTime, nullable=False),
    Column("direction", String(2)),
    Column("strike", Float),
    Column("opt_type", String(2)),
    Column("expiry", String(16)),
    Column("lots", Integer),
    Column("qty", Integer),
    Column("entry_price", Float),
    Column("exit_price", Float),
    Column("gross_pnl", Float, nullable=False),
    Column("entry_cost", Float),
    Column("exit_cost", Float),
    Column("total_cost", Float, nullable=False),
    Column("net_pnl", Float, nullable=False),
    Column("r_multiple", Float),
    Column("exit_reason", String(32)),
    Column("hold_minutes", Float),
    Column("mode", String(8), nullable=False, default="PAPER"),
    Column("cost_breakdown", JSON),
    Index("ix_journal_date", "trade_date"),
)

risk_state = Table(
    "risk_state",
    METADATA,
    Column("trade_date", String(10), primary_key=True),
    Column("mode", String(8), primary_key=True),
    Column("realized_pnl", Float, nullable=False, default=0.0),
    Column("trades_taken", Integer, nullable=False, default=0),
    Column("consecutive_losses", Integer, nullable=False, default=0),
    Column("halted", Boolean, nullable=False, default=False),
    Column("halt_reason", Text),
    Column("halted_at", TZDateTime),
    Column("manual_halt", Boolean, nullable=False, default=False),
    Column("updated_at", TZDateTime),
)

engine_events = Table(
    "engine_events",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", TZDateTime, nullable=False),
    Column("level", String(12), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("message", Text),
    Column("payload", JSON),
    Index("ix_events_ts", "ts"),
    Index("ix_events_kind", "kind"),
)

regime_history = Table(
    "regime_history",
    METADATA,
    Column("trade_date", String(10), primary_key=True),
    Column("symbol", String(32), primary_key=True),
    Column("state", String(12), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("model_hash", String(32)),
    Column("features", JSON),
    Column("computed_at", TZDateTime),
)

instruments = Table(
    "instruments",
    METADATA,
    Column("security_id", String(32), primary_key=True),
    Column("exchange_segment", String(16), primary_key=True),
    Column("symbol", String(64)),
    Column("underlying", String(32)),
    Column("instrument_type", String(16)),
    Column("expiry", String(16)),
    Column("strike", Float),
    Column("opt_type", String(2)),
    Column("lot_size", Integer),
    Column("tick_size", Float),
    Column("refreshed_at", TZDateTime),
    UniqueConstraint("underlying", "expiry", "strike", "opt_type", name="uq_instrument_contract"),
    Index("ix_instr_lookup", "underlying", "expiry", "opt_type"),
)


# ── data corpus + Backtest Lab ────────────────────────────────────────────

universe = Table(
    "universe",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key", String(48), nullable=False, unique=True),  # e.g. BANKNIFTY_OPT
    Column("label", String(96)),
    Column("category", String(16), nullable=False),  # INDEX|STOCK|FUTURES|OPTIONS|CURRENCY
    Column("series_key", String(48), nullable=False),  # price series that drives signals
    Column("yahoo_symbol", String(32)),
    Column("dhan_security_id", String(32)),
    Column("dhan_segment", String(16)),
    Column("dhan_instrument", String(16)),
    Column("lot_size", Integer, default=1),
    Column("tick_size", Float, default=0.05),
    Column("product", String(32)),  # cost product key
    Column("session_open", String(5)),
    Column("session_close", String(5)),
    Column("square_off", String(5)),
    Column("in_universe", Boolean, default=True),
    Column("extra", JSON),
    Column("updated_at", TZDateTime),
)

# OHLCV bars at any resolution ('1m','2m','5m','15m','60m','1d'), keyed by the
# price-series key (an index, a stock, a spot FX pair, or later a futures
# continuous series from Dhan).
bars = Table(
    "bars",
    METADATA,
    Column("ts", TZDateTime, primary_key=True),  # bar START (UTC on disk)
    Column("series_key", String(48), primary_key=True),
    Column("resolution", String(4), primary_key=True),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Float, default=0),
    Column("oi", Float),
    Column("source", String(8)),  # yahoo | dhan | csv
    Index("ix_bars_series_res_ts", "series_key", "resolution", "ts"),
)

param_sets = Table(
    "param_sets",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy", String(48), nullable=False),
    Column("label", String(96)),
    Column("timeframe", String(4)),
    Column("instrument_key", String(48)),
    Column("params", JSON, nullable=False),
    Column("source_run_id", Integer),
    Column("is_live", Boolean, default=False),
    Column("metrics", JSON),
    Column("created_at", TZDateTime),
)

backtest_runs = Table(
    "backtest_runs",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", TZDateTime, nullable=False),
    Column("finished_at", TZDateTime),
    Column("kind", String(16)),  # single | optimize | walkforward | research
    Column("label", String(160)),
    Column("config", JSON),
    Column("config_hash", String(16)),
    Column("status", String(12)),  # QUEUED | RUNNING | DONE | FAILED
    Column("progress", Float, default=0.0),
    Column("message", Text),
    Column("metrics", JSON),
    Column("artifacts", JSON),
)

backtest_trades = Table(
    "backtest_trades",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, nullable=False),
    Column("strategy", String(48)),
    Column("instrument_key", String(48)),
    Column("side", String(5)),
    Column("signal_ts", TZDateTime),
    Column("entry_ts", TZDateTime),
    Column("entry_price", Float),
    Column("exit_ts", TZDateTime),
    Column("exit_price", Float),
    Column("qty", Float),
    Column("gross_pnl", Float),
    Column("costs", Float),
    Column("net_pnl", Float),
    Column("ret_pct", Float),
    Column("r_multiple", Float),
    Column("exit_reason", String(24)),
    Column("strength", Float),
    Column("mae_r", Float),
    Column("mfe_r", Float),
    Column("bars_held", Integer),
    Column("detail", JSON),
    Index("ix_bt_trades_run", "run_id"),
)

app_settings = Table(
    "app_settings",
    METADATA,
    Column("key", String(64), primary_key=True),
    Column("value", JSON),
    Column("updated_at", TZDateTime),
)

# ── news + sentiment (endpoints exist now; the scorer is built separately) ──

news_items = Table(
    "news_items",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", TZDateTime, nullable=False),
    Column("source", String(64)),
    Column("headline", Text, nullable=False),
    Column("body", Text),
    Column("url", Text),
    Column("symbols", JSON),
    Column("sentiment", Float),  # -1..+1, optional
    Column("meta", JSON),
    Column("created_at", TZDateTime),
    Index("ix_news_ts", "ts"),
)

sentiment_scores = Table(
    "sentiment_scores",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", TZDateTime, nullable=False),
    Column("scope", String(16), nullable=False),  # MARKET | INDEX | STOCK | SECTOR
    Column("symbol", String(48)),
    Column("score", Float, nullable=False),  # -1..+1
    Column("confidence", Float),
    Column("horizon", String(16)),  # intraday | swing | positional
    Column("source", String(64)),
    Column("note", Text),
    Column("created_at", TZDateTime),
    Index("ix_sent_ts", "ts"),
)

# ── broker account snapshots (free-tier Trading API; cached, never polled) ──

account_cache = Table(
    "account_cache",
    METADATA,
    Column("key", String(32), primary_key=True),  # profile|funds|holdings|positions|ledger|trades
    Column("fetched_at", TZDateTime),
    Column("payload", JSON),
    Column("error", Text),
)

data_jobs = Table(
    "data_jobs",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(24), nullable=False),  # backfill | quality
    Column("status", String(12), nullable=False),
    Column("progress", Float, default=0.0),
    Column("message", Text),
    Column("created_at", TZDateTime),
    Column("finished_at", TZDateTime),
    Column("payload", JSON),
)

# ── Live Desk: multi-instrument signals and one-unit paper positions ──────

desk_signals = Table(
    "desk_signals",
    METADATA,
    Column("id", String(16), primary_key=True),
    Column("created_at", TZDateTime, nullable=False),
    Column("expires_at", TZDateTime),
    Column("resolved_at", TZDateTime),
    Column("instrument_key", String(48), nullable=False),
    Column("series_key", String(32), nullable=False),
    Column("kind", String(8)),  # INDEX | STOCK
    Column("side", String(8)),  # BUY | SELL (stock); BUY_CE | BUY_PE (index)
    Column("strategy", String(48)),
    Column("timeframe", String(6)),
    Column("bar_ts", TZDateTime),
    Column("status", String(12)),  # PENDING | TAKEN | SKIPPED | EXPIRED
    Column("payload", JSON),
    Index("ix_desk_signals_created", "created_at"),
)

desk_positions = Table(
    "desk_positions",
    METADATA,
    Column("id", String(16), primary_key=True),
    Column("signal_id", String(16)),
    Column("instrument_key", String(48), nullable=False),
    Column("series_key", String(32), nullable=False),
    Column("kind", String(8)),
    Column("side", String(8)),
    Column("strategy", String(48)),
    Column("timeframe", String(6)),
    Column("opened_at", TZDateTime, nullable=False),
    Column("closed_at", TZDateTime),
    Column("status", String(12), nullable=False),  # OPEN | CLOSED
    Column("entry_price", Float),  # per unit (share price / option premium)
    Column("exit_price", Float),
    Column("qty", Integer),
    Column("gross_pnl", Float),
    Column("costs", Float),
    Column("net_pnl", Float),
    Column("exit_reason", String(24)),
    Column("payload", JSON),
    Index("ix_desk_positions_status", "status"),
)

# ── Paper book (ADR-001): one paper trade per desk alert, never an order ──
# Money is integer PAISE (BIGINT); Decimal only at the cost-model boundary. A NO_FILL
# row keeps entry/exit NULL and gross/costs/net 0.

paper_trades = Table(
    "paper_trades",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("alert_id", String(32), nullable=False, unique=True),  # desk_signals.id
    Column("status", String(16), nullable=False),  # PENDING_FILL | OPEN | NO_FILL | CLOSED
    Column("close_reason", String(24)),  # NO_FILL | GAP_THROUGH_STOP | STOP | TARGET | ...
    Column("grade", String(8), nullable=False),  # live (dhan*) | delayed
    Column("data_source", String(32), nullable=False),
    Column("instrument_key", String(48), nullable=False),
    Column("series_key", String(32), nullable=False),
    Column("kind", String(8), nullable=False),  # STOCK | INDEX
    Column("side", String(8), nullable=False),  # BUY | SELL | BUY_CE | BUY_PE
    Column("direction", String(8), nullable=False),  # long | short (on the underlying)
    Column("strategy", String(48), nullable=False),
    Column("timeframe", String(6), nullable=False),
    Column("confidence", Float),  # alert confidence score (analytics, not money)
    Column("product", String(32), nullable=False),  # cost table: equity_intraday | index_options
    Column("qty", Integer, nullable=False),  # 1 share, or one lot of the alerted option
    Column("entry_ts", TZDateTime),
    Column("fill_bar_ts", TZDateTime),
    Column("exit_ts", TZDateTime),
    Column("last_bar_ts", TZDateTime),  # management cursor: last bar processed
    Column("resolution_min", Integer),  # desk fine resolution the alert was built on
    Column("entry_paise", BigInteger),
    Column("exit_paise", BigInteger),
    Column("gross_paise", BigInteger),
    Column("costs_paise", BigInteger),
    Column("net_paise", BigInteger),
    Column("risk_paise", BigInteger),  # excludes costs; R = net / risk
    Column("created_at", TZDateTime, nullable=False),  # the alert's created_at (data time)
    Column("expires_at", TZDateTime, nullable=False),
    Column("plan", JSON, nullable=False),  # alert snapshot + levels in paise (for US-002)
    Column("updated_at", TZDateTime, nullable=False),
    Index("ix_paper_trades_status", "status"),
    Index("ix_paper_trades_created", "created_at"),
)

paper_events = Table(
    "paper_events",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_id", Integer, ForeignKey("paper_trades.id"), nullable=False),
    Column("ts", TZDateTime, nullable=False),  # bar or decision time, never wall time
    Column("kind", String(24), nullable=False),  # OPENED | FILLED | NO_FILL | ...
    Column("detail", JSON),  # why, plus the bar prices used
    Index("ix_paper_events_trade", "trade_id"),
)


# ── Telegram outbox (US-004, ADR-002 amendments 1 and 6) ─────────────────
# Every phone message is a row before it is a request: queued -> sending (claimed and
# committed BEFORE the HTTP call) -> sent | failed | skipped. Delivery is at-least-once: a
# row found in `sending` at start-up was cut off mid-call and is queued again, flagged
# `resend`. At most one row per dedup_key and per (alert_id, kind).
telegram_outbox = Table(
    "telegram_outbox",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("dedup_key", String(160), nullable=False),
    Column("kind", String(16), nullable=False),  # alert|exit|critical|missed|digest|...
    Column("alert_id", String(64)),
    Column("payload", JSON, nullable=False),  # what the formatter reads at send time
    Column("status", String(8), nullable=False),  # queued|sending|sent|failed|skipped
    Column("attempts", Integer, nullable=False, default=0),
    Column("created_at", TZDateTime, nullable=False),
    Column("alert_created_at", TZDateTime, nullable=False),
    Column("expires_at", TZDateTime),  # None: never expires (EXIT, CRITICAL, notes)
    Column("next_attempt_at", TZDateTime),
    Column("claimed_at", TZDateTime),
    Column("silent", Boolean, nullable=False, default=False),
    Column("sent_at", TZDateTime),
    Column("message_id", Integer),
    Column("latency_ms", Integer),  # sent_at - alert_created_at, ms, never negative
    Column("last_error", Text),  # SendResult.detail: status + description, token masked
    Column("confidence", Float),  # a SIGNAL's score: the order inside a burst
    Column("resend", Boolean, nullable=False, default=False),
    UniqueConstraint("dedup_key", name="uq_telegram_outbox_dedup_key"),
    UniqueConstraint("alert_id", "kind", name="uq_telegram_outbox_alert_kind"),
    Index("ix_telegram_outbox_status_sent", "status", "sent_at"),
)
