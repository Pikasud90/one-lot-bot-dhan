"""Signal audit (what an alert would have done), scoreboard, confidence, presets, calendar, token flow."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST
from onelot.desk.audit import audit_signal, confidence, strategy_scoreboard
from onelot.lab.presets import PRESET_NAMES, all_presets, presets_for


def bars(start: str, closes: list[float], spread: float = 1.0, minutes: int = 2) -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp(start, tz=IST), periods=len(closes), freq=f"{minutes}min")
    c = np.array(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 1.0}, index=idx
    )


def sig(
    direction="long", entry=100.0, stop=95.0, target=110.0, kind="STOCK", bar="2026-10-29 10:00"
):
    return {
        "id": "x",
        "direction": direction,
        "kind": kind,
        "bar_ts": pd.Timestamp(bar, tz=IST).isoformat(),
        "strategy": "ema_cross",
        "economics": {
            "underlying_entry": entry,
            "underlying_stop": stop,
            "underlying_target": target,
            "qty": 1,
            "entry_price": entry,
            "costs_inr": 0.5,
        },
    }


class TestAudit:
    def test_target_hit(self):
        b = bars(
            "2026-10-29 09:15", [100] * 25 + [103, 106, 111, 108]
        )  # bars after 10:00 climb to 111
        out = audit_signal(sig(), b, dt.datetime(2026, 10, 29, 16, 0, tzinfo=IST), 2, intraday=True)
        assert (
            out["outcome"] == "TARGET"
            and out["r_now"] == pytest.approx(2.0)
            and out["pnl_est"] == pytest.approx(9.5)
        )
        assert out["mfe_r"] >= 2.0 and out["mae_r"] >= 0

    def test_stop_hit_and_tie_goes_to_stop(self):
        b = bars("2026-10-29 09:15", [100] * 25 + [97, 94])
        out = audit_signal(sig(), b, dt.datetime(2026, 10, 29, 16, 0, tzinfo=IST), 2, intraday=True)
        assert out["outcome"] == "STOP" and out["r_now"] == pytest.approx(-1.0)
        wide = bars("2026-10-29 09:15", [100] * 25 + [100], spread=12)  # one bar touches both
        out2 = audit_signal(
            sig(), wide, dt.datetime(2026, 10, 29, 16, 0, tzinfo=IST), 2, intraday=True
        )
        assert out2["outcome"] == "STOP"

    def test_intraday_square_off_vs_swing_open(self):
        b = bars("2026-10-29 09:15", [100] * 25 + [101, 102, 103])
        later = dt.datetime(2026, 10, 30, 10, 0, tzinfo=IST)
        intr = audit_signal(sig(), b, later, 2, intraday=True)
        swing = audit_signal(sig(), b, later, 2, intraday=False)
        assert intr["outcome"] == "SQUARE_OFF" and intr["r_now"] == pytest.approx(0.6)
        assert swing["outcome"] == "OPEN" and swing["r_now"] == pytest.approx(0.6)

    def test_short_side_and_no_data(self):
        b = bars("2026-10-29 09:15", [100] * 25 + [97, 89])
        out = audit_signal(
            sig("short", 100, 105, 90), b, dt.datetime(2026, 10, 29, 16, 0, tzinfo=IST), 2, True
        )
        assert out["outcome"] == "TARGET" and out["r_now"] == pytest.approx(2.0)
        assert (
            audit_signal(sig(), pd.DataFrame(), dt.datetime.now(IST), 2, True)["outcome"]
            == "NO_DATA"
        )

    def test_option_pnl_uses_premium_fn(self):
        b = bars("2026-10-29 09:15", [100] * 25 + [111])
        s = sig(kind="INDEX")
        s["economics"]["entry_price"] = 10.0
        s["economics"]["qty"] = 30
        out = audit_signal(
            s,
            b,
            dt.datetime(2026, 10, 29, 16, 0, tzinfo=IST),
            2,
            True,
            premium_fn=lambda u, ts: 10 + (u - 100) * 0.6,
        )
        assert out["outcome"] == "TARGET" and out["pnl_est"] == pytest.approx(6 * 30 - 0.5)

    def test_scoreboard_and_confidence(self):
        sigs = [
            {"strategy": "a", "outcome": {"outcome": "TARGET", "r_now": 2.0, "pnl_est": 100}},
            {"strategy": "a", "outcome": {"outcome": "STOP", "r_now": -1.0, "pnl_est": -50}},
            {"strategy": "a", "outcome": {"outcome": "OPEN", "r_now": 0.2}},
            {"strategy": "b", "outcome": None},
        ]
        sb = strategy_scoreboard(sigs)
        assert (
            sb["a"]["n"] == 2
            and sb["a"]["hit_rate"] == 50.0
            and sb["a"]["avg_r"] == 0.5
            and "b" not in sb
        )
        c0 = confidence(None, None, None, None)
        assert c0["score"] == 50.0 and c0["label"] == "medium"
        hi = confidence(
            90,
            {"trades": 80, "expectancy_r": 0.3, "profit_factor": 1.6},
            {"n": 20, "hit_rate": 60, "avg_r": 0.4},
            True,
        )
        lo = confidence(
            20,
            {"trades": 80, "expectancy_r": -0.3, "profit_factor": 0.7},
            {"n": 20, "hit_rate": 20, "avg_r": -0.5},
            False,
        )
        assert (
            hi["score"] > 70 and hi["label"] == "high" and lo["score"] < 35 and lo["label"] == "low"
        )
        # The label follows the stored (1-decimal) score, so a raw 64.97 stored as 65.0 is
        # 'high' and a raw 44.96 stored as 45.0 is 'medium': the floored number shown on the
        # desk title and the phone then always agrees with the label (US-003 BUG-4).
        neutral = 0.30 * 50 + 0.20 * 50 + 0.15 * 50  # the three parts without evidence
        edge_hi = confidence((64.97 - neutral) / 0.35, None, None, None)
        edge_lo = confidence((44.96 - neutral) / 0.35, None, None, None)
        assert (edge_hi["score"], edge_hi["label"]) == (65.0, "high")
        assert (edge_lo["score"], edge_lo["label"]) == (45.0, "medium")
        few = confidence(50, {"trades": 3, "expectancy_r": 0.9, "profit_factor": 9}, None, None)
        assert 50 <= few["parts"]["backtest_evidence"] < 60  # 3 trades barely move it


class TestPresets:
    def test_three_presets_per_strategy_without_exit_knobs(self):
        ps = all_presets()
        assert len(ps) >= 14
        for _name, p in ps.items():
            assert tuple(p) == PRESET_NAMES and p["default"] == {}
            for k in p["low"]:
                assert k not in ("target_r", "trail_atr_mult")
        e = presets_for("ema_cross")
        assert e["low"]["fast"] < e["high"]["fast"] and e["low"]["slow"] < e["high"]["slow"]


class TestCalendarAndToken:
    def test_2026_calendar_is_loaded_and_verified(self):
        from onelot.clock import TradingCalendar
        from onelot.settings import PROJECT_ROOT

        c = TradingCalendar.from_yaml(PROJECT_ROOT / "config" / "nse_holidays.yaml")
        assert c.verified
        assert not c.is_trading_day(dt.date(2026, 11, 10)) and c.is_trading_day(
            dt.date(2026, 11, 11)
        )
        assert c.holiday_name(dt.date(2026, 3, 3)) == "Holi"

    def test_session_token_persists_across_restarts(self, tmp_path):
        from onelot.config import Secrets

        t1 = Secrets._ephemeral_api_token(tmp_path)
        t2 = Secrets._ephemeral_api_token(tmp_path)
        assert (
            t1 == t2
            and len(t1) >= 32
            and (tmp_path / "data" / "api_token.txt").read_text().strip() == t1
        )

    def test_totp_and_token_generation_error_is_clean(self, monkeypatch):
        import httpx

        from onelot.broker.base import BrokerError
        from onelot.broker.totp import generate_access_token, totp_now

        assert (
            totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", digits=8, at=59) == "94287082"
        )  # RFC 6238 vector
        calls = {}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, params=None):
                calls["params"] = params
                return httpx.Response(401, json={"errorMessage": "Invalid TOTP"})

        monkeypatch.setattr("onelot.broker.totp.httpx.Client", FakeClient)
        with pytest.raises(BrokerError) as ei:
            generate_access_token("1100", "123456", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        assert "Invalid TOTP" in str(ei.value) and "123456" not in str(ei.value)
        assert calls["params"]["pin"] == "123456" and len(calls["params"]["totp"]) == 6
