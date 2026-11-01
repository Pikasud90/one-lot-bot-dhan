"""Builders for the Desk payloads the Telegram formatter reads (shapes from desk/desk.py)."""

from __future__ import annotations

import re
from typing import Any

ALLOWED_TAGS = ("b", "i", "code")


def stock_alert(**over: Any) -> dict[str, Any]:
    """A Desk signal dict as `Desk._raise_signal` builds it: stock BUY, live feed."""
    alert: dict[str, Any] = {
        "id": "a1b2c3d4e5f6",
        "created_at": "2026-11-02T10:15:00+05:30",
        "expires_at": "2026-11-02T10:20:00+05:30",
        "instrument_key": "RELIANCE",
        "series_key": "RELIANCE",
        "label": "Reliance Industries",
        "kind": "STOCK",
        "side": "BUY",
        "direction": "long",
        "strategy": "orb",
        "strategy_label": "Opening Range Breakout",
        "timeframe": "5m",
        "bar_ts": "2026-11-02T10:10:00+05:30",
        "strength": 71.0,
        "confidence": {"score": 72.4, "label": "high", "parts": {"signal_strength": 71.0}},
        "horizon": "intraday",
        "data": {
            "source": "dhan-ws",
            "resolution": "1m",
            "delayed": False,
            "delay_note": "live exchange feed",
            "bar_age_sec": 12.0,
        },
        "reasons": ["close above the opening range"],
        "economics": {
            "kind": "STOCK",
            "side": "BUY",
            "qty": 1,
            "unit_label": "1 share",
            "underlying_entry": 2950.5,
            "underlying_stop": 2921.0,
            "underlying_target": 3009.5,
            "entry_price": 2950.5,
            "stop_price": 2921.0,
            "target_price": 3009.5,
            "outlay": 2950.5,
            "risk_inr": 31.2,
            "reward_inr": 57.3,
            "costs_inr": 1.7,
            "rr_net": 1.84,
            "r_target": 2.0,
            "option": {},
            "synthetic": False,
            "notes": [],
        },
        "text": "BUY 1 share Reliance Industries at ~₹2,950.50 (market)",
        "status": "PENDING",
        "risk_allowed": True,
        "risk_reason": "",
        "outcome": None,
        "manage": {"target_r": 2.0, "exit_time": "15:15", "tf_min": 5, "horizon": "intraday"},
    }
    alert.update(over)
    return alert


def index_alert(opt_type: str = "CE", *, synthetic: bool = False, **over: Any) -> dict[str, Any]:
    """A Desk signal dict for a BANKNIFTY option; live-chain premium unless `synthetic`."""
    alert = stock_alert(
        id="c3d4e5f6a7b8",
        instrument_key="BANKNIFTY",
        series_key="BANKNIFTY",
        label="NIFTY BANK",
        kind="INDEX",
        side=f"BUY_{opt_type}",
        direction="long" if opt_type == "CE" else "short",
        confidence={"score": 66.0, "label": "high", "parts": {}},
    )
    option: dict[str, Any] = {
        "strike": 45000.0,
        "opt_type": opt_type,
        "expiry": "2026-11-25",
        "delta": 0.55,
        "iv": 0.14,
        "premium": 312.4,
        "lot": 30,
    }
    if not synthetic:
        option.update({"bid": 311.9, "ask": 312.4, "spread_pct": 0.16, "oi": 120000})
    alert["economics"] = {
        "kind": "INDEX",
        "side": f"BUY_{opt_type}",
        "qty": 30,
        "unit_label": "1 lot (30)",
        "underlying_entry": 45120.0,
        "underlying_stop": 44980.0,
        "underlying_target": 45400.0,
        "entry_price": 312.4,
        "stop_price": 250.1,
        "target_price": 380.0,
        "outlay": 9372.0,
        "risk_inr": 1900.0,
        "reward_inr": 2000.0,
        "costs_inr": 120.0,
        "rr_net": 1.05,
        "r_target": 2.0,
        "option": option,
        "synthetic": synthetic,
        "notes": ["premium is a Black-76 estimate"] if synthetic else ["live chain quote"],
    }
    alert.update(over)
    return alert


def exit_event(advice: str = "STOP", **position_over: Any) -> dict[str, Any]:
    """The Desk EXIT payload: {"advice": str, "position": <desk position dict>}."""
    position: dict[str, Any] = {
        "id": "f00dbabe1234",
        "signal_id": "c3d4e5f6a7b8",
        "instrument_key": "BANKNIFTY",
        "series_key": "BANKNIFTY",
        "label": "NIFTY BANK",
        "kind": "INDEX",
        "side": "BUY_CE",
        "direction": "long",
        "strategy": "orb",
        "timeframe": "5m",
        "opened_at": "2026-11-02T10:16:00+05:30",
        "qty": 30,
        "entry_price": 312.4,
        "entry_underlying": 45120.0,
        "stop_underlying": 44980.0,
        "target_underlying": 45400.0,
        "initial_stop_underlying": 44980.0,
        "option": {
            "strike": 45000.0,
            "opt_type": "CE",
            "expiry": "2026-11-25",
            "premium": 312.4,
            "lot": 30,
        },
        "manage": {"exit_time": "15:15"},
        "horizon": "intraday",
        "trail_active": False,
        "bars_held": 5,
        "sessions_held": 1,
        "last_underlying": 44975.0,
        "unrealized": -1234.5,
        "r_now": -1.036,
        "exit_advice": advice,
        "hold_check": "monitoring",
        "status": "OPEN",
        "source": "dashboard",
        "notes": [],
        "confidence": {"score": 66.0, "label": "high"},
    }
    position.update(position_over)
    return {"advice": advice, "position": position}


def assert_telegram_html(msg: str) -> None:
    """Telegram HTML we may send: <= 12 lines, <= 600 chars, only balanced b/i/code tags,
    nothing else that looks like markup, and every '&' starts an entity."""
    assert len(msg) <= 600, len(msg)
    assert len(msg.split("\n")) <= 12
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([^<>]*)>", msg):
        closing, name = m.group(1), m.group(2)
        assert name in ALLOWED_TAGS, f"tag {name!r} in {msg!r}"
        if closing:
            assert stack and stack.pop() == name, f"unbalanced </{name}> in {msg!r}"
        else:
            stack.append(name)
    assert not stack, f"unclosed {stack} in {msg!r}"
    bare = re.sub(r"</?(?:b|i|code)>", "", msg)
    assert "<" not in bare and ">" not in bare, msg
    assert re.fullmatch(r"(?:[^&]|&(?:amp|lt|gt|quot);)*", bare, flags=re.S), msg
