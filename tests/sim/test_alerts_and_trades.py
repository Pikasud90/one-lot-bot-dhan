"""US-075 AC2 to AC7 on a stock whose history is flat (every true range 2.00, so ATR(14) of
60m bars is exactly 2.00 and the first risk 1.5 x 2 = 3.00): the alert's timing, take, skip,
expiry, the ratchet minute by minute, stops, gaps, strategy exits and the session end."""

from __future__ import annotations

import pandas as pd
import pytest

from onelot.clock import IST
from onelot.manage import ratchet

from .conftest import MemBars, flat_minutes, make_manager, set_bar, trading_days

DAYS = trading_days("2025-01-06", 15)
D = DAYS[-2]
SLIP = 0.0003  # the Lab's slippage_pct, 0.03 %


def T(hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{D} {hhmm}", tz=IST)


def frames():
    rel = flat_minutes("2025-01-06", 15, 1000.0, 1.0, volume=1000.0)
    nifty = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    return rel, nifty


def start(db_engine, tmp_path, rel, nifty, at="10:00", params=None):
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    out = m.create(
        {"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m", "mode": "past",
         "start": T(at).isoformat(), "params": params or {}}
    )  # fmt: skip
    return m, m.get(out["session"]["id"])


def until(s, hhmm: str) -> list[dict]:
    """Advance minute by minute until the minute starting at `hhmm` is revealed."""
    events: list[dict] = []
    while pd.Timestamp(s.cursor) < T(hhmm):
        events += s.advance(1)["events"]
    return events


def taken(db_engine, tmp_path, force, rel=None, nifty=None, params=None, **signals):
    """A session with a long alert on the candle 10:10 to 10:15, taken and filled at 10:15."""
    r0, n0 = frames()
    force(long=[f"{D} 10:10"], **signals)
    rel = rel if rel is not None else r0
    nifty = nifty if nifty is not None else n0
    m, s = start(db_engine, tmp_path, rel, nifty, params=params)
    until(s, "10:14")
    s.take(s.alert["id"])
    fill = s.advance(1)
    return m, s, fill


# ── AC2 ───────────────────────────────────────────────────────────────────────
def test_an_alert_appears_only_when_its_candle_has_closed(db_engine, tmp_path, force):
    rel, nifty = frames()
    force(long=[f"{D} 10:10"])  # the 5m candle 10:10 to 10:15
    _, s = start(db_engine, tmp_path, rel, nifty)
    while pd.Timestamp(s.cursor) < T("10:13"):
        r = s.advance(1)
        assert r["events"] == [] and r["alert"] is None
    r = s.advance(1)  # reveals 10:14, the candle's last minute
    assert r["cursor"] == T("10:14").isoformat()
    assert [e["kind"] for e in r["events"]] == ["ALERT"]
    assert r["events"][0]["ts"] == T("10:15").isoformat()
    a = r["alert"]
    assert a["side"] == "LONG" and a["action"] == "BUY" and a["qty"] == 1
    assert (a["entry"], a["risk"], a["stop"], a["target"]) == (1000.0, 3.0, 997.0, 1009.0)
    assert a["expires"] == T("10:25").isoformat()


# ── AC3 ───────────────────────────────────────────────────────────────────────
def test_take_fills_at_the_next_open_with_slippage(db_engine, tmp_path, force):
    rel, nifty = frames()
    set_bar(rel, f"{D} 10:15", 1002.0, 1003.0, 1001.0, 1002.0)
    force(long=[f"{D} 10:10"])
    _, s = start(db_engine, tmp_path, rel, nifty)
    until(s, "10:14")
    st = s.take(s.alert["id"])
    assert st["trade"]["status"] == "PENDING_FILL" and st["alert"] is None
    r = s.advance(1)
    filled = [e for e in r["events"] if e["kind"] == "FILLED"]
    assert len(filled) == 1 and filled[0]["ts"] == T("10:15").isoformat()
    t = r["trade"]
    assert t["status"] == "OPEN" and t["entry_ts"] == T("10:15").isoformat()
    assert t["entry_price"] == round(1002.0 * (1 + SLIP), 2) == t["entry"]
    assert t["stop0"] == round(1002.0 * (1 + SLIP) - 3.0, 2)


def test_skip_clears_the_alert_and_an_untaken_alert_expires_after_10_minutes(
    db_engine, tmp_path, force
):
    rel, nifty = frames()
    force(long=[f"{D} 10:10", f"{D} 10:40"])
    _, s = start(db_engine, tmp_path, rel, nifty)
    until(s, "10:14")
    st = s.skip(s.alert["id"])
    assert st["alert"] is None and st["trade"] is None
    ev = until(s, "10:44")
    assert [e["kind"] for e in ev] == ["ALERT"] and ev[0]["ts"] == T("10:45").isoformat()
    before = until(s, "10:53")
    assert before == [] and s.alert is not None
    r = s.advance(1)  # 10:54: the tenth minute after the alert
    assert [e["kind"] for e in r["events"]] == ["ALERT_EXPIRED"]
    assert r["events"][0]["ts"] == T("10:55").isoformat() and r["alert"] is None


def test_take_without_an_alert_is_refused(db_engine, tmp_path, force):
    from onelot.sim.engine import SimError

    rel, nifty = frames()
    force()
    _, s = start(db_engine, tmp_path, rel, nifty)
    with pytest.raises(SimError) as e:
        s.take("a_1")
    assert e.value.status == 409


# ── AC4 ───────────────────────────────────────────────────────────────────────
def rising_then_falling(rel: pd.DataFrame) -> None:
    """From 10:16 up 0.5 a minute for 40 minutes, then down 1.0 a minute."""
    price = 1000.0
    t = T("10:16")
    for _ in range(40):
        nxt = price + 0.5
        set_bar(rel, str(t.tz_localize(None)), price, nxt + 0.2, price - 0.2, nxt)
        price, t = nxt, t + pd.Timedelta(minutes=1)
    for _ in range(60):
        nxt = price - 1.0
        set_bar(rel, str(t.tz_localize(None)), price, price + 0.2, nxt - 0.2, nxt)
        price, t = nxt, t + pd.Timedelta(minutes=1)


def test_the_stop_and_target_follow_the_ratchet_minute_by_minute(db_engine, tmp_path, force):
    rel, nifty = frames()
    rising_then_falling(rel)
    _, s, fill = taken(db_engine, tmp_path, force, rel=rel, nifty=nifty)
    plan = s.trade.plan
    got: list[tuple] = []
    minutes = list(fill["minutes"])
    events = list(fill["events"])
    while s.trade is not None:
        r = s.advance(1)
        minutes += r["minutes"]
        events += r["events"]
    for e in events:
        if e["kind"] == "STAGE":
            got.append(("STAGE", e["data"]["stage"], e["ts"]))
        elif e["kind"] == "STOP_MOVED":
            got.append(("STOP_MOVED", e["data"]["stop"], e["ts"]))
        elif e["kind"] == "TARGET_MOVED":
            got.append(("TARGET_MOVED", e["data"]["target"], e["ts"]))
        elif e["kind"] == "CLOSED":
            got.append(("CLOSED", e["data"]["reason"], e["ts"]))
    # the same rule run by hand on the same one-minute bars, the fill minute first
    want: list[tuple] = []
    state = ratchet.start(plan)
    p = ratchet.RatchetParams(extend=True)
    for ts, o, h, lo, _c, _v in minutes:
        end = pd.Timestamp(ts + 60, unit="s", tz="UTC").tz_convert(IST).isoformat()
        hit = ratchet.exit_on_bar(plan, state, o, h, lo)
        if hit:
            want.append(("CLOSED", hit[0], end))
            break
        state, changes = ratchet.advance(plan, state, h, lo, p)
        for ch in changes:
            if ch in ("BREAKEVEN", "LOCKED", "TRAILING"):
                want.append(("STAGE", ch, end))
            elif ch == "STOP_MOVED":
                want.append(("STOP_MOVED", round(state.stop, 2), end))
            else:
                want.append(("TARGET_MOVED", round(state.target, 2), end))
    assert got == want
    stages = [g[1] for g in got if g[0] == "STAGE"]
    assert stages == ["BREAKEVEN", "LOCKED", "TRAILING"]
    assert any(g[0] == "TARGET_MOVED" for g in got) and got[-1][:2] == ("CLOSED", "TRAIL_STOP")
    last = s.closed[0]
    assert last["exit_reason"] == "TRAIL_STOP"
    assert last["exit_price"] == round(state.stop * (1 - SLIP), 2)


# ── AC5 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bar, reason, price",
    [
        ((1000.0, 1000.5, 997.0, 998.0), "STOP", 997.3 * (1 - SLIP)),  # a touch: at the stop
        ((996.0, 996.5, 995.0, 996.0), "GAP_STOP", 996.0 * (1 - SLIP)),  # a gap: at the open
        ((1000.0, 1010.0, 997.0, 1005.0), "STOP", 997.3 * (1 - SLIP)),  # both: a stop
    ],
)
def test_a_minute_touching_the_stop_closes_the_trade(
    db_engine, tmp_path, force, bar, reason, price
):
    rel, nifty = frames()
    set_bar(rel, f"{D} 10:17", *bar)
    _, s, _ = taken(db_engine, tmp_path, force, rel=rel, nifty=nifty)
    assert s.trade.plan.stop == pytest.approx(997.3)
    s.advance(1)  # 10:16, flat
    r = s.advance(1)  # 10:17
    closed = [e for e in r["events"] if e["kind"] == "CLOSED"]
    assert len(closed) == 1 and closed[0]["data"]["reason"] == reason
    t = s.closed[0]
    assert t["exit_reason"] == reason and t["exit_price"] == round(price, 2)
    assert r["trade"] is None


# ── AC6 ───────────────────────────────────────────────────────────────────────
def test_a_strategy_exit_closes_at_the_next_open_with_auto_exit(db_engine, tmp_path, force):
    _, s, _ = taken(db_engine, tmp_path, force, long_exit=[f"{D} 10:25"])
    ev = until(s, "10:29")
    assert [e["kind"] for e in ev] == ["EXIT_SIGNAL"] and ev[0]["ts"] == T("10:30").isoformat()
    r = s.advance(1)
    closed = [e for e in r["events"] if e["kind"] == "CLOSED"]
    assert (
        closed[0]["data"]["reason"] == "SIGNAL_EXIT" and closed[0]["ts"] == T("10:30").isoformat()
    )
    assert s.closed[0]["exit_price"] == round(1000.0 * (1 - SLIP), 2)


def test_without_auto_exit_only_the_signal_is_raised_and_close_works(db_engine, tmp_path, force):
    _, s, _ = taken(
        db_engine, tmp_path, force, params={"auto_exit": False}, long_exit=[f"{D} 10:25"]
    )
    ev = until(s, "10:35")
    assert [e["kind"] for e in ev] == ["EXIT_SIGNAL"] and s.trade is not None
    s.close()
    r = s.advance(1)
    closed = [e for e in r["events"] if e["kind"] == "CLOSED"]
    assert closed[0]["data"]["reason"] == "MANUAL" and closed[0]["ts"] == T("10:36").isoformat()


# ── AC7 ───────────────────────────────────────────────────────────────────────
def test_the_session_exit_time_closes_the_trade_and_the_day_ends(db_engine, tmp_path, force):
    _, s, _ = taken(db_engine, tmp_path, force)
    ev = until(s, "15:15")
    closed = [e for e in ev if e["kind"] == "CLOSED"]
    assert (
        closed[0]["data"]["reason"] == "SESSION_END" and closed[0]["ts"] == T("15:15").isoformat()
    )
    r = s.advance(390)
    assert r["day_end"] is True and r["cursor"] == T("15:29").isoformat()
    assert r["events"][-1]["kind"] == "DAY_END"
    r = s.advance(1)
    nxt = pd.Timestamp(f"{DAYS[-1]} 09:15", tz=IST)
    assert r["cursor"] == nxt.isoformat() and r["day_end"] is False
