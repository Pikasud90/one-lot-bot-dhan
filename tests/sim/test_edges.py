"""US-083: what the first run on real data and the edge-case hunt turned up. One test for each
defect fixed; the numbers come from the same flat synthetic history as the other sim tests."""

from __future__ import annotations

import pandas as pd
import pytest

from onelot.clock import IST
from onelot.sim.engine import SimError

from .conftest import MemBars, flat_minutes, make_manager
from .test_alerts_and_trades import DAYS, D, T, frames, start, until
from .test_api_and_persistence import BODY, H, app_engine, client, strict  # noqa: F401


# ── the request's numbers and flags ───────────────────────────────────────────
@pytest.mark.parametrize("minutes", [1.5, True, False, "5", None, [1], {}, -1, 0, 391])
def test_minutes_must_be_a_whole_number_from_1_to_390(app_engine, minutes):  # noqa: F811
    c = client(app_engine)
    sid = strict(c.post("/api/sim/sessions", json=BODY, headers=H))["session"]["id"]
    r = c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": minutes}, headers=H)
    assert r.status_code == 400 and "whole number" in r.json()["detail"]
    assert strict(c.get(f"/api/sim/sessions/{sid}"))["session"]["cursor"] == T("09:59").isoformat()


def test_a_whole_float_is_accepted_and_no_body_means_one_minute(app_engine):  # noqa: F811
    c = client(app_engine)
    sid = strict(c.post("/api/sim/sessions", json=BODY, headers=H))["session"]["id"]
    r = strict(c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 3.0}, headers=H))
    assert len(r["minutes"]) == 3
    r = strict(c.post(f"/api/sim/sessions/{sid}/advance", headers=H))
    assert len(r["minutes"]) == 1


@pytest.mark.parametrize(
    ("params", "word"),
    [
        ({"capital": 10**30}, "capital"),
        ({"capital": -5}, "capital"),
        ({"capital": "nan"}, "capital"),
        ({"capital": True}, "capital"),
        ({"stop_atr_k": 1e9}, "stop_atr_k"),
        ({"stop_atr_k": 0}, "stop_atr_k"),
        ({"target_r": 1000}, "target_r"),
        ({"extend": "false"}, "extend"),
        ({"auto_exit": "no"}, "auto_exit"),
        ({"auto_exit": 0}, "auto_exit"),
        ({"stop_atr_tf": "5m"}, "stop_atr_tf"),
    ],
)
def test_a_session_setting_that_is_not_sane_is_a_400(app_engine, params, word):  # noqa: F811
    r = client(app_engine).post("/api/sim/sessions", json={**BODY, "params": params}, headers=H)
    assert r.status_code == 400 and word in r.json()["detail"]


def test_the_settings_a_page_sends_are_kept(app_engine):  # noqa: F811
    p = {"capital": 250000, "stop_atr_k": 2, "target_r": 2.5, "extend": False, "auto_exit": False}
    r = strict(client(app_engine).post("/api/sim/sessions", json={**BODY, "params": p}, headers=H))
    assert r["session"]["params"]["extend"] is False and r["session"]["capital"] == 250000


def test_a_start_on_the_very_first_stored_minute_says_what_is_needed(app_engine):  # noqa: F811
    first = pd.Timestamp(f"{DAYS[0]} 09:15", tz=IST).isoformat()
    c = client(app_engine)
    r = c.post("/api/sim/sessions", json={**BODY, "start": first}, headers=H)
    assert r.status_code == 400 and "one stored minute before" in r.json()["detail"]
    second = pd.Timestamp(f"{DAYS[0]} 09:16", tz=IST).isoformat()
    assert c.post("/api/sim/sessions", json={**BODY, "start": second}, headers=H).status_code == 200


# ── a hole in the store longer than the horizon ───────────────────────────────
def gap_frames():
    """Five days in January, five in April: 3 months with no bars at all."""
    a = flat_minutes("2025-01-06", 5, 1000.0, 1.0, volume=1000.0)
    b = flat_minutes("2025-04-07", 5, 1000.0, 1.0, volume=1000.0)
    n = flat_minutes("2025-01-06", 5, 25000.0, 10.0)
    n2 = flat_minutes("2025-04-07", 5, 25000.0, 10.0)
    return pd.concat([a, b]), pd.concat([n, n2])


def test_a_gap_in_the_store_longer_than_the_horizon_is_crossed_not_the_end(db_engine, tmp_path):
    rel, nifty = gap_frames()
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    # a start inside the gap: the first bars after it are 2 months away
    out = m.create({"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m",
                    "mode": "past", "start": "2025-02-10T10:00:00+05:30"})  # fmt: skip
    s = m.get(out["session"]["id"])
    assert s.cursor == "2025-01-10T15:29:00+05:30"
    r = s.advance(1)
    assert r["minutes"][0][0] == int(pd.Timestamp("2025-04-07 09:15", tz=IST).timestamp())
    assert r["over"] is False
    # a session riding to the last January bar goes on into April, not DATA_END
    out = m.create({"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m",
                    "mode": "past", "start": "2025-01-10T15:00:00+05:30"})  # fmt: skip
    s = m.get(out["session"]["id"])
    r = s.advance(390)
    assert r["day_end"] is True and r["over"] is False
    r = s.advance(1)
    assert r["over"] is False and r["cursor"] == "2025-04-07T09:15:00+05:30"


# ── take near the end of the day ──────────────────────────────────────────────
def test_a_take_at_1513_fills_at_1514_and_a_take_at_1514_is_too_late(db_engine, tmp_path, force):
    rel, nifty = frames()
    force(long=[f"{D} 15:05"])  # the 5m candle 15:05 to 15:10
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    out = m.create({"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m",
                    "mode": "past", "start": T("15:00").isoformat()})  # fmt: skip
    s = m.get(out["session"]["id"])
    until(s, "15:13")
    assert s.alert is not None and s.alert["ts"] == T("15:10").isoformat()
    # the cursor is 15:13: a take fills at 15:14's open, one minute before the exit time
    assert s.cursor == T("15:13").isoformat()
    st = s.take(s.alert["id"])
    assert st["trade"]["status"] == "PENDING_FILL"
    s.advance(1)
    assert s.trade is not None and s.trade.status == "OPEN"
    assert s.trade.entry_ns == int(T("15:14").value)
    r = s.advance(1)  # 15:15: the exit time
    closed = [e for e in r["events"] if e["kind"] == "CLOSED"]
    assert closed and closed[0]["data"]["exit_reason"] == "SESSION_END"


def test_a_take_with_the_cursor_at_1514_is_refused(db_engine, tmp_path, force):
    rel, nifty = frames()
    force(long=[f"{D} 15:05"])
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    out = m.create({"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m",
                    "mode": "past", "start": T("15:00").isoformat()})  # fmt: skip
    s = m.get(out["session"]["id"])
    until(s, "15:14")
    assert s.alert is not None and s.cursor == T("15:14").isoformat()
    with pytest.raises(SimError) as e:
        s.take(s.alert["id"])
    assert e.value.status == 409 and "too late" in e.value.message


# ── a short evening session (Diwali Muhurat) ──────────────────────────────────
def test_a_short_session_ends_the_day_on_its_last_minute_and_closes_the_trade(
    db_engine, tmp_path, force
):
    rel, nifty = frames()
    last = DAYS[-2]
    day = pd.Timestamp(f"{last} 00:00", tz=IST)
    keep = (
        (rel.index < day)
        | (rel.index >= day + pd.Timedelta(days=1))
        | (
            (rel.index >= pd.Timestamp(f"{last} 13:45", tz=IST))
            & (rel.index < pd.Timestamp(f"{last} 14:45", tz=IST))
        )
    )
    rel, nifty = rel[keep], nifty[keep]
    force(long=[f"{last} 14:00"])
    m = make_manager(db_engine, MemBars({"RELIANCE": rel, "NIFTY": nifty}), tmp_path)
    out = m.create({"instrument": "RELIANCE", "strategy": "ema_cross", "timeframe": "5m",
                    "mode": "past", "start": f"{last}T13:45:00+05:30"})  # fmt: skip
    s = m.get(out["session"]["id"])
    assert s.cursor == f"{DAYS[-3]}T15:29:00+05:30"
    while s.alert is None:
        s.advance(1)
    s.take(s.alert["id"])
    r = s.advance(390)
    assert r["day_end"] is True and r["cursor"] == f"{last}T14:44:00+05:30"
    kinds = [e["kind"] for e in r["events"]]
    assert kinds[-2:] == ["CLOSED", "DAY_END"] and r["trade"] is None
    assert s.closed[0]["exit_reason"] == "SESSION_END"
    assert s.closed[0]["exit_ts"] == f"{last}T14:45:00+05:30"
    assert len(r["minutes"]) < 60 and r["over"] is False


# ── India VIX missing: say so in plain words ──────────────────────────────────
class VixBars(MemBars):
    def __init__(self, frames, vix_days):
        super().__init__(frames)
        self.vix_days = vix_days

    def daily_bars(self, key):
        if key != "INDIAVIX" or self.vix_days is None:
            return None
        idx = pd.DatetimeIndex([pd.Timestamp(d, tz=IST) for d in self.vix_days])
        return pd.DataFrame({"open": 14.0, "high": 15.0, "low": 13.0, "close": 14.0,
                             "volume": 0.0}, index=idx)  # fmt: skip


def _nifty(db_engine, tmp_path, vix_days, at):
    rel, nifty = frames()
    m = make_manager(db_engine, VixBars({"NIFTY": nifty, "RELIANCE": rel}, vix_days), tmp_path)
    out = m.create({"instrument": "NIFTY", "strategy": "ema_cross", "timeframe": "5m",
                    "mode": "past", "start": T(at).isoformat()})  # fmt: skip
    return m.get(out["session"]["id"])


def test_a_missing_vix_is_said_in_the_premium_note(db_engine, tmp_path):
    s = _nifty(db_engine, tmp_path, None, "10:00")
    assert "India VIX is not stored" in s.premium_note()


def test_a_vix_that_ends_before_the_cursor_day_is_said_in_the_premium_note(db_engine, tmp_path):
    s = _nifty(db_engine, tmp_path, [str(DAYS[0]), str(DAYS[3])], "10:00")
    note = s.premium_note()
    assert "India VIX is stored only to" in note and DAYS[3].strftime("%d %b %Y") in note
    s = _nifty(db_engine, tmp_path, [str(DAYS[0]), str(D)], "10:00")
    assert "VIX" not in s.premium_note()  # stored up to the day: nothing to warn about
    stock = frames()
    assert stock  # a share never needs VIX


# ── nothing happens silently ──────────────────────────────────────────────────
def _alerted(db_engine, tmp_path, force):
    rel, nifty = frames()
    force(long=[f"{D} 10:10"])
    m, s = start(db_engine, tmp_path, rel, nifty)
    until(s, "10:14")
    assert s.alert is not None
    return m, s


def test_closing_a_trade_before_its_fill_is_a_cancelled_event_and_never_a_result(
    db_engine, tmp_path, force
):
    m, s = _alerted(db_engine, tmp_path, force)
    taken = s.take(s.alert["id"])
    assert taken["events"] == [] and taken["minutes"] == []
    tid = taken["trade"]["id"]
    r = s.close()
    assert r["trade"] is None and r["minutes"] == []
    assert [(e["kind"], e["data"]) for e in r["events"]] == [("CANCELLED", {"trade_id": tid})]
    assert set(r["events"][0]) == {"seq", "ts", "kind", "text", "data"}
    assert r["summary"]["trades"] == 0 and r["trades"] == []
    rows = m.store.trades(session_id=s.setup.id)
    assert [(x["id"], x["status"]) for x in rows] == [(tid, "CANCELLED")]
    assert m.trades() == []  # never in the list of results nor the CSV
    assert m.trades_csv().count("\n") == 1
    # a new manager rebuilds the session without a trade, and the events are stored
    m2 = make_manager(db_engine, m.store_bars, tmp_path)
    s2 = m2.get(s.setup.id)
    assert s2.trade is None and s2.closed == []


def test_close_of_an_open_trade_reveals_nothing_and_end_reveals_the_minute_and_the_close(
    db_engine, tmp_path, force
):
    m, s = _alerted(db_engine, tmp_path, force)
    s.take(s.alert["id"])
    s.advance(3)  # filled at 10:15, open
    r = s.close()
    assert r["events"] == [] and r["minutes"] == [] and r["trade"]["status"] == "OPEN"
    e = s.end()
    assert len(e["minutes"]) == 1 and e["session"]["status"] == "ENDED"
    assert [x["kind"] for x in e["events"]] == ["CLOSED"]
    assert e["events"][0]["data"]["exit_reason"] == "MANUAL" and e["trade"] is None
    assert e["minutes"][0][0] == int(pd.Timestamp(e["session"]["cursor"]).timestamp())
    seqs = [x["seq"] for x in e["events"]]
    assert seqs == sorted(set(seqs))


def test_end_with_a_trade_not_yet_filled_is_a_cancelled_event(db_engine, tmp_path, force):
    _, s = _alerted(db_engine, tmp_path, force)
    s.take(s.alert["id"])
    e = s.end()
    assert [x["kind"] for x in e["events"]] == ["CANCELLED"] and e["minutes"] == []
    assert e["summary"]["trades"] == 0


def test_the_http_answers_of_take_skip_close_and_end_carry_events_and_minutes(app_engine, force):  # noqa: F811
    force(long=[f"{D} 10:10"])
    c = client(app_engine)
    body = {**BODY, "start": T("10:00").isoformat()}
    sid = strict(c.post("/api/sim/sessions", json=body, headers=H))["session"]["id"]
    a = strict(c.post(f"/api/sim/sessions/{sid}/advance", json={"minutes": 15}, headers=H))["alert"]
    r = strict(c.post(f"/api/sim/sessions/{sid}/take", json={"alert_id": a["id"]}, headers=H))
    assert r["events"] == [] and r["minutes"] == []
    r = strict(c.post(f"/api/sim/sessions/{sid}/close", json={}, headers=H))
    assert [e["kind"] for e in r["events"]] == ["CANCELLED"]
    r = strict(c.post(f"/api/sim/sessions/{sid}/end", json={}, headers=H))
    assert r["events"] == [] and r["minutes"] == []
