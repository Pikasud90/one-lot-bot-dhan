"""High-level backtest API used by the web app, the CLI, the optimizer and the research gate.

    BacktestConfig  -> run_backtest(cfg, cache) -> BacktestResult

One instrument per run (the one-lot rule makes portfolio constraints moot:
there is never more than one unit on, and no sizing to interact). Multi-
instrument research loops call this once per instrument.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import pandas as pd
from sqlalchemy import Engine

from ..clock import IST
from ..config import CostConfig
from ..costs import CostModel
from ..data.db import get_engine
from ..data.store import load_bars
from ..data.universe import InstrumentSpec, load_specs, market_index_key
from ..settings import current_settings
from ..strategies.base import Context
from ..strategies.registry import get_strategy
from .execution import (
    OptionPricer,
    daily_equity_on_calendar,
    equity_from_trades,
    execute_trade,
    thirty_percent_rule,
)
from .metrics import all_metrics, breakdown
from .simulator import SimTrade, simulate


@dataclass
class BacktestConfig:
    strategy: str
    instrument_key: str
    params: dict = field(default_factory=dict)
    timeframe: str = "5m"
    start: str | None = None  # ISO date (inclusive)
    end: str | None = None  # ISO date (inclusive)
    slippage_pct: float = 0.03
    cost_multiplier: float = 1.0
    use_fine_path: bool = True
    warmup_sessions: int = 3
    reference_capital: float = 100000.0
    label: str = ""
    # sentiment gate (see lab/sentiment.py); "off" leaves the strategy untouched
    sentiment_mode: str = "off"
    sentiment_scope: str = "MARKET"
    sentiment_symbol: str | None = None
    sentiment_threshold: float = 0.2
    sentiment_max_age_days: float = 5.0

    def key(self) -> str:
        d = asdict(self)
        d.pop("label", None)
        return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:12]

    @classmethod
    def from_settings(cls, strategy: str, instrument_key: str, **kw) -> BacktestConfig:
        s = current_settings().backtest
        base = {
            "slippage_pct": s.slippage_pct,
            "use_fine_path": s.use_fine_path,
            "warmup_sessions": s.warmup_sessions,
            "reference_capital": s.reference_capital_inr,
            "timeframe": s.default_timeframe,
        }
        base.update(kw)
        return cls(strategy=strategy, instrument_key=instrument_key, **base)


@dataclass
class BacktestResult:
    config: BacktestConfig
    instrument: InstrumentSpec
    trades: pd.DataFrame
    equity: pd.Series
    metrics: dict
    signal_counts: dict
    candidates: int
    days: pd.DatetimeIndex
    bars: pd.DataFrame | None = None  # the underlying bars (for the chart)
    features: pd.DataFrame | None = None

    def chart_payload(self, max_bars: int = 6000) -> dict:
        """What the browser needs to draw candles + trade markers."""
        out: dict = {"candles": [], "trades": []}
        if self.bars is not None and not self.bars.empty:
            b = self.bars.iloc[-max_bars:]
            out["candles"] = [
                [int(ts.timestamp()), float(o), float(h), float(l), float(c)]
                for ts, o, h, l, c in zip(
                    b.index, b["open"], b["high"], b["low"], b["close"], strict=True
                )
            ]
        if not self.trades.empty:
            for r in self.trades.to_dict("records"):
                d = r.get("detail") or {}
                out["trades"].append(
                    {
                        "entry_ts": int(pd.Timestamp(r["entry_ts"]).timestamp()),
                        "exit_ts": int(pd.Timestamp(r["exit_ts"]).timestamp()),
                        "side": r["side"],
                        "entry": d.get("underlying_entry", r["entry_price"]),
                        "exit": d.get("underlying_exit", r["exit_price"]),
                        "net_pnl": r["net_pnl"],
                        "r": r["r_multiple"],
                        "reason": r["exit_reason"],
                    }
                )
        return out


class DataCache:
    """Loads bars once per (series, resolution) per process."""

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine or get_engine()
        self._bars: dict[tuple[str, str], pd.DataFrame] = {}
        self.features: dict[tuple, pd.DataFrame] = {}
        self.series, self.instruments = load_specs()

    def bars(self, series_key: str, res: str) -> pd.DataFrame:
        k = (series_key, res)
        if k not in self._bars:
            df = load_bars(self.engine, series_key, res)
            spec = self.series.get(series_key)
            if spec is not None and not spec.has_volume and "volume" in df.columns:
                # An index or spot FX has no traded volume, and the live desk drops whatever
                # the feed sends for it (has_volume=False). Dhan's index history carries a
                # volume figure only from 2025-12-03, so without this every volume filter
                # would switch on part-way through a backtest and the years would not compare.
                df = df.assign(volume=0.0)
            self._bars[k] = df
        return self._bars[k]

    def market_symbol(self) -> str:
        return market_index_key()

    def vix(self) -> pd.Series:
        df = self.bars("INDIAVIX", "1d")
        if df.empty:
            return pd.Series(dtype=float)
        s = df["close"].copy()
        s.index = s.index.normalize()
        return s

    def spec(self, instrument_key: str) -> InstrumentSpec:
        try:
            return self.instruments[instrument_key.upper()]
        except KeyError as exc:
            raise KeyError(
                f"unknown instrument {instrument_key!r}; see config/universe.yaml"
            ) from exc


def _window(
    df: pd.DataFrame, start: dt.date | None, end: dt.date | None, warmup_sessions: int
) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    if df.empty:
        return df, None
    days = pd.DatetimeIndex(sorted(set(df.index.normalize())))
    start_day = (
        days[min(warmup_sessions, len(days) - 1)] if start is None else pd.Timestamp(start, tz=IST)
    )
    pos = days.searchsorted(start_day)
    warm_day = days[max(0, pos - warmup_sessions)]
    end_ts = pd.Timestamp(end, tz=IST) + pd.Timedelta(days=1) if end else None
    sl = (
        df.loc[warm_day:] if end_ts is None else df.loc[warm_day : end_ts - pd.Timedelta(seconds=1)]
    )
    return sl, start_day


def _session_params(spec: InstrumentSpec, params: dict) -> dict:
    """Instrument session defaults (currency trades 09:00-17:00) unless the user set them."""
    s = spec.session
    out = dict(params)
    out.setdefault("entry_start", s.get("first_entry", "09:30"))
    out.setdefault("entry_end", s.get("last_entry", "14:45"))
    out.setdefault("exit_time", s.get("square_off", "15:15"))
    return out


def generate_for_instrument(cfg: BacktestConfig, cache: DataCache, strategy=None):
    spec = cache.spec(cfg.instrument_key)
    params = _session_params(spec, cfg.params)
    strategy = strategy or get_strategy(cfg.strategy, {**params, "timeframe": cfg.timeframe})
    bars = cache.bars(spec.series_key, cfg.timeframe)
    start = dt.date.fromisoformat(cfg.start) if cfg.start else None
    end = dt.date.fromisoformat(cfg.end) if cfg.end else None
    win, start_day = _window(bars, start, end, cfg.warmup_sessions)
    if win.empty or len(win) < 60:
        return spec, strategy, None, None, start_day
    mkt_key = cache.market_symbol()
    market = cache.bars(mkt_key, cfg.timeframe) if mkt_key != spec.series_key else pd.DataFrame()
    ctx = Context(
        symbol=spec.series_key,
        daily=cache.bars(spec.series_key, "1d"),
        market=market if not market.empty else None,
        extras={
            "fcache": cache.features,
            "fkey": (spec.series_key, cfg.timeframe, win.index[0], win.index[-1]),
            "market_daily": cache.bars(mkt_key, "1d"),
            "has_volume": bool(cache.series[spec.series_key].has_volume)
            if spec.series_key in cache.series
            else True,
        },
    )
    f = strategy.generate(win, ctx)
    fine = None
    if cfg.use_fine_path and cfg.timeframe != "1m":
        for res in ("1m", "2m"):
            if res == cfg.timeframe:
                break
            fine_all = cache.bars(spec.series_key, res)
            if not fine_all.empty:
                fine = fine_all.loc[win.index[0] : win.index[-1] + pd.Timedelta(minutes=60)]
                break
    return spec, strategy, f, fine, start_day


def run_backtest(
    cfg: BacktestConfig,
    cache: DataCache | None = None,
    progress: Callable[[float, str], None] | None = None,
    keep_bars: bool = False,
) -> BacktestResult:
    cache = cache or DataCache()
    settings = current_settings()
    spec, strategy, f, fine, start_day = generate_for_instrument(cfg, cache)
    counts = {"setups_long": 0, "setups_short": 0, "signals_long": 0, "signals_short": 0}
    if progress:
        progress(0.1, f"{spec.key}: signals")
    candidates: list[SimTrade] = []
    days = pd.DatetimeIndex([])
    test = None
    if f is not None and cfg.sentiment_mode and cfg.sentiment_mode != "off":
        from ..data.resample import minutes_of
        from .sentiment import apply_gate, bias_at, load_scores

        scores = load_scores(cache.engine, cfg.sentiment_scope, cfg.sentiment_symbol)
        bias = bias_at(
            scores,
            f.index,
            minutes_of(cfg.timeframe) if cfg.timeframe != "1d" else 375,
            cfg.sentiment_threshold,
            cfg.sentiment_max_age_days,
        )
        f = apply_gate(f, bias, cfg.sentiment_mode)
        counts["sentiment_scores_used"] = int(len(scores))
        counts["sentiment_blocked_long"] = int(f["blk_sentiment_long"].sum())
        counts["sentiment_blocked_short"] = int(f["blk_sentiment_short"].sum())
    if f is not None:
        test = f.loc[start_day:] if start_day is not None else f
        counts["setups_long"] = int(test["setup_long"].sum())
        counts["setups_short"] = int(test["setup_short"].sum())
        counts["signals_long"] = int(test["long_entry"].sum())
        counts["signals_short"] = int(test["short_entry"].sum())
        days = pd.DatetimeIndex(sorted(set(test.index.normalize())))
        candidates = simulate(
            f, strategy.p, spec.series_key, cfg.strategy, cfg.slippage_pct, fine, start_day
        )
    if progress:
        progress(0.6, f"{spec.key}: execution")

    cost_cfg = CostConfig.load(settings.project_root / "config" / "costs.yaml").product(
        spec.product
    )
    costs = CostModel(cost_cfg)
    if cfg.cost_multiplier != 1.0:
        costs = _ScaledCosts(costs, cfg.cost_multiplier)
    pricer = OptionPricer(settings.backtest, cache.vix()) if spec.is_option else None
    rows = [
        execute_trade(t, spec, costs, settings.backtest, cfg.slippage_pct, pricer)
        for t in candidates
    ]
    trades = pd.DataFrame(rows)
    daily = equity_from_trades(trades, cfg.reference_capital)
    equity = daily_equity_on_calendar(daily, days, cfg.reference_capital) if len(days) else daily
    metrics = all_metrics(trades, equity, cfg.reference_capital)
    metrics["candidates"] = len(candidates)
    metrics.update(counts)
    metrics["instrument"] = spec.key
    metrics["category"] = spec.category
    metrics["unit"] = 1 if spec.category == "STOCK" else spec.lot_size
    metrics["pricing"] = "black76_synthetic" if spec.is_option else "underlying"
    metrics["thirty_pct_rule"] = thirty_percent_rule(trades)
    metrics["costs_unverified"] = not cost_cfg.verified_against_contract_note
    if progress:
        progress(1.0, "done")
    return BacktestResult(
        cfg,
        spec,
        trades,
        equity,
        metrics,
        counts,
        len(candidates),
        days,
        bars=(test[["open", "high", "low", "close"]] if keep_bars and test is not None else None),
        features=(test if keep_bars else None),
    )


class _ScaledCosts:
    """Cost stress: every leg's charges multiplied (research 'costs x2')."""

    def __init__(self, inner: CostModel, mult: float) -> None:
        self.inner, self.mult = inner, mult

    def leg(self, premium: float, qty: int, side: str):
        b = self.inner.leg(premium, qty, side)
        return type(b)(**{**b.__dict__, "total": b.total * self.mult})


def summarize(res: BacktestResult) -> str:
    m = res.metrics
    c = res.config
    lo = res.days.min().date() if len(res.days) else "-"
    hi = res.days.max().date() if len(res.days) else "-"
    lines = [
        f"Backtest {c.strategy} on {res.instrument.key} ({res.instrument.category}, "
        f"unit={m.get('unit')}) | {c.timeframe} | {lo} .. {hi}"
    ]
    for k, label in (
        ("trades", "Trades"),
        ("win_rate", "Win rate %"),
        ("profit_factor", "Profit factor"),
        ("expectancy_r", "Expectancy (R)"),
        ("expectancy", "Expectancy (Rs)"),
        ("net_pnl", "Net P&L (Rs)"),
        ("costs", "Costs (Rs)"),
        ("max_drawdown_inr", "Max DD (Rs)"),
        ("sharpe", "Sharpe"),
        ("payoff", "Payoff"),
        ("max_consec_losses", "Max consec losses"),
    ):
        v = m.get(k)
        lines.append(
            f"  {label:<20} {v if v is None else (round(v, 3) if isinstance(v, float) else v)}"
        )
    lines.append(
        f"  setups L/S {res.signal_counts['setups_long']}/{res.signal_counts['setups_short']} -> "
        f"signals {res.signal_counts['signals_long']}/{res.signal_counts['signals_short']} -> "
        f"trades {m.get('trades', 0)}"
    )
    tp = m.get("thirty_pct_rule") or {}
    if tp.get("payoff") is not None:
        lines.append(
            f"  30% rule: payoff {tp['payoff']} (need {tp['payoff_needed_for_30pct']}), "
            f"PF at 30% WR = {tp['pf_at_30pct_win_rate']} -> {tp['verdict']}"
        )
    return "\n".join(lines)


def per_breakdowns(res: BacktestResult) -> dict[str, list[dict]]:
    out = {}
    for by in ("exit_reason", "hour", "weekday", "strength_bucket", "side"):
        b = breakdown(res.trades, by)
        out[by] = b.reset_index().to_dict("records") if not b.empty else []
    return out
