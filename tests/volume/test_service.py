"""US-089 part 3: the service on a fixture store (AC1, AC2, AC3, AC6)."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from onelot.notify.base import NotificationLevel
from onelot.volume import indicator as ind
from onelot.volume.service import VolumeError, VolumeWatch, build_baselines, compute_view

from .conftest import ABSENT, BASKET, DAYS, HALF_DAY, SPIKE_DAY, Recorder, at, desk_cfg

NOW_KEYS = {"minute", "turnover_1m", "expected_1m", "rvol_1m", "rvol_5m", "rvol_day", "z",
            "state", "flow_share_5m", "flow_share_day", "turnover_day", "expected_day",
            "net_flow_day", "up", "down", "flat", "text"}  # fmt: skip
TOP_KEYS = {"as_of", "session", "market_open", "source", "basket", "now", "minutes", "index",
            "stocks", "indices", "alerts", "book", "settings", "notes", "index_options",
            "building"}  # fmt: skip
ALERT_KEYS = {"id", "ts", "scope", "key", "rvol", "z", "flow_share", "direction", "leaders",
              "indices", "title", "text", "evidence"}  # fmt: skip


@pytest.fixture(scope="session")
def shared(store_template):
    """One watch over the fixture store for the read-only tests (its baselines are cached)."""
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{store_template}")
    yield stored_watch(engine)
    engine.dispose()


def stored_watch(store, **volume) -> VolumeWatch:
    after = at(SPIKE_DAY + dt.timedelta(days=3), "08:00")
    return VolumeWatch(store, None, desk_cfg=desk_cfg(**volume), sync=True, clock=lambda: after)


def direct_expected(frames, day: dt.date, col: int, window: str = "1m") -> float:
    """The median, over the previous 20 full sessions, of the BASKET's turnover in the window
    ending at `col` (one minute, or the day so far), straight from the bars."""
    sums: dict[dt.date, float] = {}
    minute = ind.OPEN_MINUTE + col
    for k in BASKET:
        df = frames[k]
        t = df["volume"] * df["close"]
        idx = pd.DatetimeIndex(df.index)
        tod = idx.hour * 60 + idx.minute
        lo = minute if window == "1m" else ind.OPEN_MINUTE
        part = t[(tod >= lo) & (tod <= minute)].groupby(idx.date[(tod >= lo) & (tod <= minute)])
        for d, v in part.sum().items():
            sums[d] = sums.get(d, 0.0) + float(v)
    counts = pd.Series(pd.DatetimeIndex(frames["SBIN"].index).date).value_counts()
    full = sorted(d for d, n in counts.items() if n >= 300 and d < day)[-20:]
    return float(np.median([sums[d] for d in full]))


def test_past_session_has_the_contract_keys_and_hand_numbers(shared, frames):
    w = shared
    day = DAYS[40]  # ten sessions after the half day
    body = w.body(day.isoformat())
    json.dumps(body, allow_nan=False)
    assert set(body) == TOP_KEYS
    assert set(body["now"]) == NOW_KEYS
    assert body["source"] == "stored" and body["session"] == day.isoformat()
    assert body["basket"] == {"count": 4, "label": "4 large stocks", "missing": [ABSENT]}
    assert len(body["minutes"]) == 375 and len(body["minutes"][0]) == 8
    assert body["now"]["minute"] == "15:29"
    last = pd.Timestamp(dt.datetime.combine(day, dt.time(15, 29)), tz="Asia/Kolkata")
    turn = sum(float(frames[k].loc[last, "volume"] * frames[k].loc[last, "close"]) for k in BASKET)
    assert body["now"]["turnover_1m"] == pytest.approx(turn, abs=1)
    assert body["now"]["expected_1m"] == pytest.approx(direct_expected(frames, day, 374), abs=1)
    assert body["now"]["expected_day"] == pytest.approx(
        direct_expected(frames, day, 374, "day"), rel=1e-9
    )
    m0 = body["minutes"][0]
    assert m0[0] == int(
        pd.Timestamp(dt.datetime.combine(day, dt.time(9, 15)), tz="Asia/Kolkata").timestamp()
    )
    assert m0[1] == pytest.approx(m0[3] + m0[4], abs=2)  # buying + selling = turnover
    assert [s["key"] for s in body["stocks"]] == sorted(
        BASKET, key=lambda k: -next(s["turnover_day"] for s in body["stocks"] if s["key"] == k)
    )
    assert sum(s["share"] for s in body["stocks"]) == pytest.approx(1.0, abs=0.01)
    assert body["index"]["key"] == "NIFTY" and len(body["index"]["minutes"]) == 375
    nifty = next(i for i in body["indices"] if i["key"] == "NIFTY")
    assert nifty["r2"] > 0.95  # the fixture's NIFTY is the basket
    assert {k for k, _ in nifty["top"]} == set(BASKET)
    bank = next(i for i in body["indices"] if i["key"] == "BANKNIFTY")
    assert bank["top"][0][0] == "SBIN" and bank["top"][0][1] == pytest.approx(1.0, abs=0.01)
    assert body["now"]["up"] + body["now"]["down"] + body["now"]["flat"] == 3


def test_half_day_is_never_a_baseline(shared, frames):
    w = shared
    day = DAYS[31]  # right after the 60-bar session with 50 times the volume
    body = w.body(day.isoformat())
    assert body["now"]["expected_1m"] == pytest.approx(direct_expected(frames, day, 374), abs=1)
    first = w.body(DAYS[0].isoformat())  # no history at all
    assert first["now"]["expected_1m"] == 0
    assert first["now"]["rvol_1m"] is None and first["now"]["z"] is None
    assert HALF_DAY == DAYS[30]


def test_spike_day_alerts_are_recomputed(shared):
    body = shared.body(SPIKE_DAY.isoformat())
    alerts = body["alerts"]
    assert alerts, "the fixture's spike must alert"
    for a in alerts:  # the study has no fact for a single stock: no evidence key at all
        want = ALERT_KEYS if a["scope"] == "MARKET" else ALERT_KEYS - {"evidence"}
        assert set(a) == want
        assert a.get("evidence", "x").strip()
    market = {a["id"]: a for a in alerts if a["scope"] == "MARKET"}
    first = market[f"v_{SPIKE_DAY:%Y%m%d}_1055_MARKET"]  # the spike's first minute
    assert first["direction"] == "SELLING" and first["z"] >= 2.5
    assert first["title"].startswith("Volume watch: ") and "mostly selling" in first["title"]
    assert "NIFTY 50" in first["text"]
    assert [a["ts"] for a in alerts] == sorted((a["ts"] for a in alerts), reverse=True)
    stock = [a for a in alerts if a["scope"] == "STOCK"]
    assert {a["key"] for a in stock} <= set(BASKET) and stock


def test_bad_and_unknown_dates(shared):
    w = shared
    with pytest.raises(VolumeError) as e:
        w.body("24-11-2026")
    assert e.value.status == 400 and "2026-11-24" in e.value.message
    with pytest.raises(VolumeError) as e:
        w.body("2030-01-01")
    assert str(DAYS[0]) in e.value.message and str(DAYS[-1]) in e.value.message


def test_default_body_when_closed_is_the_last_stored_session(shared):
    body = shared.body()
    assert body["session"] == SPIKE_DAY.isoformat() and body["source"] == "stored"
    assert body["notes"][0].startswith("The market is closed")


def test_missing_stock_and_no_bars():
    """A stock without bars goes to `missing`; the rest still answers."""
    from .conftest import make_bars

    sb = make_bars(1, 500.0, spike=False)
    base = build_baselines(
        SPIKE_DAY, None, ["SBIN", "INFY"], [], lambda k: sb if k == "SBIN" else None
    )
    today = sb.loc[pd.DatetimeIndex(sb.index).date == SPIKE_DAY]
    view = compute_view(SPIKE_DAY, {"SBIN": today}, base, ["SBIN", "INFY"], "clv")
    assert view is not None and view.missing == ["INFY"] and view.keys == ["SBIN"]
    assert compute_view(SPIKE_DAY, {}, base, ["SBIN"], "clv") is None


# ── live: the poll step (AC3) ────────────────────────────────────────────
class FakeDesk:
    fine_minutes = 1

    def __init__(self, frames, upto: str, open_: bool = True) -> None:
        cut = pd.Timestamp(
            dt.datetime.combine(SPIKE_DAY, dt.time.fromisoformat(upto)), tz="Asia/Kolkata"
        )
        self.trackers = {k: SimpleNamespace(bars=v[v.index <= cut]) for k, v in frames.items()}
        self.context_bars: dict = {}
        self.open = open_
        self.cfg = desk_cfg()

    def _market_open(self, now):
        return self.open

    def _data_api_active(self):
        return False

    def _adapter_getter(self):
        return None


def live_watch(store, frames, upto="10:59", notifier=None, **volume):
    clock = at(SPIKE_DAY, "11:00")
    desk = FakeDesk(frames, upto)
    w = VolumeWatch(
        store, desk, notifier, desk_cfg=desk_cfg(**volume), sync=True, clock=lambda: clock
    )
    return w, clock


def test_live_alert_raised_once_and_sent_as_a_warning(store, frames):
    rec = Recorder()
    w, now = live_watch(store, frames, notifier=rec)
    w.on_poll(now, True)
    assert rec.sent, "the spike must alert"
    n = rec.sent[0]
    assert n.level is NotificationLevel.WARNING and n.kind == "volume"
    assert n.title.startswith("Volume watch")
    ids = [a["id"] for a in w.alerts()["alerts"]]
    assert len(ids) == len(set(ids)) == len(rec.sent)
    w.on_poll(now, True)  # the same minute: nothing
    w.on_poll(now + dt.timedelta(minutes=1), True)  # nothing new closed
    assert len(rec.sent) == len(ids)
    body = w.body()
    assert body["source"] == "live" and body["market_open"] is True
    assert {a["id"] for a in body["alerts"]} == set(ids)
    assert body["now"]["minute"] == "10:59"
    # a restart on the same database repeats nothing
    rec2 = Recorder()
    w2, _ = live_watch(store, frames, notifier=rec2)
    w2.on_poll(now, True)
    assert rec2.sent == []


def test_cap_and_switches(store, frames):
    rec = Recorder()
    w, now = live_watch(store, frames, notifier=rec, max_alerts_per_day=1)
    w.on_poll(now, True)
    assert len(rec.sent) == 1 and rec.sent[0].payload["scope"] == "MARKET"


def test_notify_false_stores_but_sends_nothing(store, frames):
    rec = Recorder()
    w, now = live_watch(store, frames, notifier=rec, notify=False)
    w.on_poll(now, True)
    assert rec.sent == [] and w.alerts()["alerts"]
    assert w.body()["alerts"]


def test_alerts_off_decides_nothing(store, frames):
    rec = Recorder()
    w, now = live_watch(store, frames, notifier=rec, alerts_on=False)
    w.on_poll(now, True)
    assert rec.sent == [] and w.alerts()["alerts"] == []


def test_cooldown_holds_a_second_market_alert(store, frames):
    rec = Recorder()
    w, now = live_watch(store, frames, notifier=rec, upto="10:57")
    w.on_poll(now, True)
    w.desk.trackers = FakeDesk(frames, "10:59").trackers
    w.on_poll(now + dt.timedelta(minutes=2), True)
    market = [n for n in rec.sent if n.payload["scope"] == "MARKET"]
    assert len(market) == 1


def test_alert_decisions_do_not_look_ahead(shared, frames):
    """The alerts of a session cut after a minute are the full session's alerts up to it."""
    w = shared
    full = w.body(SPIKE_DAY.isoformat())["alerts"]
    base = w.baselines(SPIKE_DAY, wait=True)
    for upto in ("10:56", "11:30", "14:00"):
        cut = pd.Timestamp(
            dt.datetime.combine(SPIKE_DAY, dt.time.fromisoformat(upto)), tz="Asia/Kolkata"
        )
        bars = {
            k: frames[k][(frames[k].index <= cut) & (frames[k].index.date == SPIKE_DAY)]
            for k in BASKET
        }
        view = compute_view(SPIKE_DAY, bars, base, list(BASKET) + [ABSENT], "clv")
        got = [w.alert_obj(view, base, c)["id"] for c in w._pick(view, 0, [])]
        assert got == [a["id"] for a in reversed(full) if a["ts"][11:16] <= upto]


def test_now_is_null_before_the_first_closed_minute(store, frames):
    """A desk holding only a pre-open bar of today: `now` is null, every key is present."""
    w, now = live_watch(store, frames, upto="09:14")
    pre = pd.Timestamp(dt.datetime.combine(SPIKE_DAY, dt.time(9, 8)), tz="Asia/Kolkata")
    for t in w.desk.trackers.values():
        old = t.bars[t.bars.index.date < SPIKE_DAY]
        extra = old.iloc[-1:].copy()
        extra.index = pd.DatetimeIndex([pre]).tz_convert(old.index.tz)
        t.bars = pd.concat([old, extra])
    body = w.body()
    assert set(body) == TOP_KEYS and body["now"] is None
    assert body["source"] == "live" and body["session"] == SPIKE_DAY.isoformat()
    assert body["minutes"] == [] and body["stocks"] == [] and body["notes"]
    json.dumps(body, allow_nan=False)


def test_index_choice_and_options(shared):
    keys = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY"]
    for k in ("banknifty", "MIDCPNIFTY"):
        body = shared.body(DAYS[-2].isoformat(), k)
        assert body["index"]["key"] == k.upper()
    assert [o["key"] for o in body["index_options"]] == keys
    assert body["index_options"][0] == {"key": "NIFTY", "label": "NIFTY 50"}
    with pytest.raises(VolumeError) as e:
        shared.body(DAYS[-2].isoformat(), "RELIANCE")
    assert e.value.status == 400 and "MIDCPNIFTY" in e.value.message


def test_settings_name_the_rule_in_use(shared):
    assert shared.body(DAYS[40].isoformat())["settings"]["rule"] == shared.cfg.rule
