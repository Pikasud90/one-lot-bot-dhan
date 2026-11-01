"""A Lab strategy as the live direction source, and the engine-side wiring it depends on."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST
from onelot.config import LiveAlphaCfg
from onelot.strategy.lab_source import LabDirectionSource


def _bars(n_days: int = 8, interval: int = 5, seed: int = 3, drift: float = 0.0006):
    rng = np.random.default_rng(seed)
    ts = []
    day = pd.Timestamp("2026-09-01", tz=IST)
    while len({t.date() for t in ts}) < n_days:
        if day.weekday() < 5:
            ts.extend(
                pd.date_range(
                    day + pd.Timedelta(hours=9, minutes=15),
                    periods=375 // interval,
                    freq=f"{interval}min",
                )
            )
        day += pd.Timedelta(days=1)
    n = len(ts)
    c = 50000 * np.exp(np.cumsum(rng.normal(drift, 0.0012, n)))
    o = np.r_[50000, c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0005, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0005, n)))  # noqa: E741
    return np.array([t.to_pydatetime() for t in ts], dtype=object), o, h, l, c


def test_rejects_timeframe_not_multiple_of_bar_interval():
    with pytest.raises(ValueError):
        LabDirectionSource(
            LiveAlphaCfg(source="lab", lab_strategy="ema_cross", lab_timeframe="12m"), 5
        )


def test_same_timeframe_evaluates_last_closed_bar():
    src = LabDirectionSource(
        LiveAlphaCfg(
            source="lab",
            lab_strategy="ema_cross",
            lab_timeframe="5m",
            lab_params={"adx_min": 0, "rsi_margin": 0, "confirm_atr": 0},
        ),
        5,
    )
    ts, o, h, l, c = _bars()
    v = src.evaluate(ts, o, h, l, c)
    assert v.strategy == "ema_cross" and v.bar_ts is not None
    assert isinstance(v.ce_fired, bool) and isinstance(v.pe_fired, bool)
    assert not (v.ce_fired and v.pe_fired)


def test_resampled_timeframe_only_uses_complete_bars():
    src = LabDirectionSource(
        LiveAlphaCfg(source="lab", lab_strategy="ema_cross", lab_timeframe="15m"), 5
    )
    ts, o, h, l, c = _bars()
    # drop the last bar so the latest 15m bucket is incomplete (2 of 3 five-minute bars)
    v = src.evaluate(ts[:-1], o[:-1], h[:-1], l[:-1], c[:-1])
    assert v.bar_ts is not None
    # the verdict's bar must end at or before the last 5m close
    last_close = pd.Timestamp(ts[-2]).tz_convert(IST) + pd.Timedelta(minutes=5)
    assert pd.Timestamp(v.bar_ts) + pd.Timedelta(minutes=15) <= last_close


def test_fires_only_on_the_fresh_bar_and_carries_a_structural_stop():
    src = LabDirectionSource(
        LiveAlphaCfg(
            source="lab",
            lab_strategy="ema_cross",
            lab_timeframe="5m",
            lab_params={"adx_min": 0, "rsi_margin": 0, "confirm_atr": 0},
        ),
        5,
    )
    # a driftless walk crosses its EMAs repeatedly; a strong trend never would
    ts, o, h, l, c = _bars(n_days=12, drift=0.0, seed=11)
    fired = []
    for i in range(300, len(c)):
        v = src.evaluate(ts[:i], o[:i], h[:i], l[:i], c[:i])
        if v.ce_fired or v.pe_fired:
            fired.append((i, v))
    assert fired, "the synthetic trend never produced a cross"
    for i, v in fired:
        sp = v.ce_stop_points if v.ce_fired else v.pe_stop_points
        assert sp is not None and sp > 0
        # fresh means the signal bar IS the latest closed bar
        assert pd.Timestamp(v.bar_ts) == pd.Timestamp(ts[i - 1]).tz_convert(IST)


def test_too_little_history_is_reported_not_raised():
    src = LabDirectionSource(
        LiveAlphaCfg(source="lab", lab_strategy="supertrend_vol", lab_timeframe="5m"), 5
    )
    ts, o, h, l, c = _bars(n_days=1)
    v = src.evaluate(ts[:30], o[:30], h[:30], l[:30], c[:30])
    assert not v.ce_fired and not v.pe_fired and "need 60" in v.reason


class TestAlphaEngineWithLabSource:
    def test_alpha_engine_builds_lab_source_from_config(self, cfg, cost_model):
        from onelot.strategy.alpha import AlphaEngine

        live = cfg.strategy.live.model_copy(
            update={"source": "lab", "lab_strategy": "supertrend_vol", "lab_timeframe": "5m"}
        )
        c2 = cfg.model_copy(update={"strategy": cfg.strategy.model_copy(update={"live": live})})
        eng = AlphaEngine(c2, cost_model)
        assert eng.lab is not None and eng.lab.name == "supertrend_vol"
        assert AlphaEngine(cfg, cost_model).lab is None

    def test_gate_report_shows_lab_gate_instead_of_trend_momentum(
        self, cfg, cost_model, mock_broker, enriched_chain
    ):
        from onelot.analytics.ivp import IVPercentileEngine
        from onelot.analytics.regime import rule_based_regime
        from onelot.strategy.alpha import AlphaEngine, EvaluationContext

        live = cfg.strategy.live.model_copy(
            update={"source": "lab", "lab_strategy": "ema_cross", "lab_timeframe": "5m"}
        )
        c2 = cfg.model_copy(update={"strategy": cfg.strategy.model_copy(update={"live": live})})
        eng = AlphaEngine(c2, cost_model)
        ts, o, h, l, c = _bars(n_days=6)
        now = dt.datetime.combine(dt.date(2026, 9, 9), dt.time(11, 0), tzinfo=IST)
        regime = rule_based_regime(c[-300:])
        ivp = IVPercentileEngine("off", 252, 60).compute([], 0.15)
        ctx = EvaluationContext(
            now=now,
            bar_ts=now,
            session_window=None,
            health_may_signal=True,
            health_reasons=[],
            index_high=h,
            index_low=l,
            index_close=c,
            index_open=o,
            index_ts=ts,
            chain_rows=enriched_chain.snapshot.rows,
            chain_age_sec=1.0,
            regime=regime,
            ivp=ivp,
            expiry=mock_broker.expiry_date.isoformat(),
            lot_size=35,
            risk_allowed=True,
            risk_reason="",
            risk_state="ACTIVE",
            open_positions=0,
        )
        out = eng.evaluate(ctx)
        gates = {g["gate"] for g in out.all_gate_dicts()}
        assert "CE:lab_strategy" in gates and "PE:lab_strategy" in gates
        assert "CE:trend" not in gates and "CE:momentum" not in gates
