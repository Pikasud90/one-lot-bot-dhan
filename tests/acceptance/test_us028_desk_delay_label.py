"""US-028 AC1 through the Desk: alerts served by the Yahoo fallback must not be labelled live.

Scenario under test: the Desk runs on a `CompositeBars` whose Dhan primary returns an empty
frame for one series, so the Yahoo fallback serves that series (``served_by[key] == "yahoo"``)
while Dhan serves the rest. Yahoo bars run about 15 minutes behind the exchange, so:

* the alert for the Yahoo-served instrument says ``data.delayed is True`` with the 15 minute
  note, and the alert for a Dhan-served instrument says ``False`` / "live exchange feed";
* each tracker row's ``health.delayed`` follows its own series' provider;
* the Desk header (``snapshot["source"]``) is delayed iff any tracker is, and lists exactly
  those keys in ``delayed_keys``;
* tracker health uses the per-tracker flag: a 15-minute-old Yahoo bar is not stale, the same
  bar from Dhan is;
* the Telegram tag (US-003) will read ``alert["data"]["delayed"]`` only, so the notification
  payload must carry the very same flag.

Everything is offline: both bar sources are fakes, and the network is made impossible.
Expected values are literals (never computed with `is_delayed_source`) so a wrong rule cannot
hide behind itself.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import json
import re
import shutil
import socket
import subprocess
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from onelot.api.server import create_app
from onelot.broker.base import Bar
from onelot.clock import IST, now_ist
from onelot.config import AppConfig
from onelot.desk import Desk
from onelot.desk.config import ContextCfg, DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.data import COLUMNS, CompositeBars, DhanBars, YahooBars
from onelot.desk.tracker import Verdict
from onelot.notify.base import Notification
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo

KEYS = ("BANKNIFTY", "SBIN")  # the two trackers of the harness desk, in sorted order
BAR_MIN = 2  # fine bar length of the harness feed (minutes)


# ── offline bar sources ───────────────────────────────────────────────────────────────
def session_bars(base: float, seed: int, end: pd.Timestamp, days: int = 12) -> pd.DataFrame:
    """`days` NSE sessions of 2-minute bars (09:15..15:29) ending at the latest weekday on or
    before `end`; only bars that have closed by `end` exist. Deterministic for a given seed."""
    rng = np.random.default_rng(seed)
    day, sessions = end.normalize(), []
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day -= pd.Timedelta(days=1)
    idx: list[pd.Timestamp] = []
    for d in sorted(sessions):
        idx.extend(
            pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=188, freq=f"{BAR_MIN}min")
        )
    idx = pd.DatetimeIndex([t for t in idx if t + pd.Timedelta(minutes=BAR_MIN) <= end])
    n = len(idx)
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.0012, n)))
    open_ = np.r_[base, close[:-1]]
    spread = np.abs(rng.normal(0, 0.0012, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.integers(1000, 5000, n).astype(float),
        },
        index=idx,
    )


def feed_end() -> pd.Timestamp:
    """Yesterday 15:30 IST: a full set of closed sessions whatever the wall clock says."""
    return (pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=1)) + pd.Timedelta(
        hours=15, minutes=30
    )


def make_frames(with_vix: bool = False) -> dict[str, pd.DataFrame]:
    end = feed_end()
    frames = {
        "BANKNIFTY": session_bars(54000.0, seed=11, end=end),
        "SBIN": session_bars(950.0, seed=12, end=end),
    }
    if with_vix:  # a context-strip series: shown on the desk, never traded
        frames["INDIAVIX"] = session_bars(15.0, seed=13, end=end)
    return frames


class OfflineDhan:
    """A Dhan-like primary. It returns bars for the keys in `serves` and an EMPTY FRAME for
    every other series it is asked about, exactly like Dhan with no data for an instrument."""

    delayed = False
    resolution = f"{BAR_MIN}m"  # the harness feed is 2-minute bars
    last_error = None

    def __init__(self, frames: dict[str, pd.DataFrame], serves, name: str = "dhan") -> None:
        self.name = name
        self.frames = frames
        self.serves = set(serves)

    def can_serve(self, spec) -> bool:
        return True

    def fetch(self, specs, days, since=None):
        return {
            k: self.frames[k] if k in self.serves else pd.DataFrame(columns=COLUMNS) for k in specs
        }


class FakeDhanAdapter:
    """Just enough of the Dhan adapter for the REAL DhanBars class: `historical_intraday`
    returns bars for the series in `serves`, none for the others (Dhan has no data for the
    instrument) and raises for the series in `fail` (a Dhan API error)."""

    def __init__(self, frames: dict[str, pd.DataFrame], serves, fail=()) -> None:
        self.frames, self.serves, self.fail = frames, set(serves), set(fail)

    def historical_intraday(self, contract, interval, start, end):
        key = contract.symbol
        if key in self.fail:
            raise RuntimeError("DH-904 rate limit exceeded")
        if key not in self.serves:
            return []
        return [
            Bar(ts.to_pydatetime(), r.open, r.high, r.low, r.close, r.volume)
            for ts, r in self.frames[key].iterrows()
        ]


class OfflineYahoo(YahooBars):
    """The real YahooBars class (name "yahoo", delayed True) with the network cut off."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        super().__init__("2m")
        self.frames = frames
        self.requested: list[list[str]] = []

    def fetch(self, specs, days, since=None):
        self.requested.append(sorted(specs))
        return {k: self.frames[k] for k in specs if k in self.frames}


class FlakyYahoo(OfflineYahoo):
    """OfflineYahoo that can go down like a real Yahoo outage: no bars and a last_error."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        super().__init__(frames)
        self.down = False

    def fetch(self, specs, days, since=None):
        if self.down:
            self.requested.append(sorted(specs))
            self.last_error = "ConnectionError: yahoo is down"
            return {}
        self.last_error = None
        return super().fetch(specs, days, since)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, n, dedup_key=None):
        self.sent.append(n)
        return {"fake": True}

    def info(self, title, body="", dedup_key=None):
        return self.send(Notification(title=title, body=body))


class OpenClock:
    """A session window that is always open with entries allowed."""

    class W:
        def __init__(self, now):
            self.open_at = now - dt.timedelta(hours=1)
            self.close_at = now + dt.timedelta(hours=3)
            self.mandatory_squareoff_at = now + dt.timedelta(hours=2)

        def is_open(self, ts):
            return True

        def entries_allowed(self, ts):
            return True

        def past_squareoff(self, ts):
            return False

    def current_window(self, ts=None):
        return OpenClock.W(ts or now_ist())


class ClosedWindow(OpenClock.W):
    """A session window that is shut: market closed, no entries."""

    def is_open(self, ts):
        return False

    def entries_allowed(self, ts):
        return False


# ── fixtures and builders ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("US-028 tests must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr("yfinance.download", refuse)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'us028.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def small_cfg() -> DeskConfig:
    return DeskConfig(
        poll_seconds=60,
        history_days=10,
        source="yahoo",
        signal_valid_minutes=10,
        context=(),
        indices=(
            DeskInstrumentCfg(
                "BANKNIFTY", "BANKNIFTY_OPT", "INDEX", (StrategySlotCfg("ema_cross", "10m"),)
            ),
        ),
        stocks=(
            DeskInstrumentCfg(
                "SBIN",
                "SBIN",
                "STOCK",
                (StrategySlotCfg("ema_cross", "10m"), StrategySlotCfg("supertrend_vol", "6m")),
            ),
        ),
    )


def new_desk(app_cfg, db, notifier=None, desk_cfg=None) -> tuple[Desk, FakeNotifier]:
    repo = Repo(db)
    gov = RiskGovernor(
        app_cfg.risk, app_cfg.account.capital_inr, app_cfg.mode, now_ist().date().isoformat(), repo
    )
    notifier = notifier or FakeNotifier()
    desk = Desk(
        db.engine, app_cfg, notifier, gov, OpenClock(), repo=repo, desk_cfg=desk_cfg or small_cfg()
    )
    return desk, notifier


def make_desk(
    app_cfg,
    db,
    monkeypatch,
    serves,
    primary_name="dhan",
    *,
    yahoo_cls=None,
    dhan_factory=None,
    with_vix=False,
    expect_served="derive",
):
    """A polled Desk on a CompositeBars.

    serves is None  -> no Dhan primary at all (a Yahoo-only desk, as without a Dhan plan);
    serves is a set -> a Dhan primary that has bars for those keys and none for the others,
                       so the Yahoo fallback serves the rest.
    with_vix adds the INDIAVIX context series (key in `serves` -> Dhan serves it, else Yahoo);
    yahoo_cls swaps the fallback (default OfflineYahoo); dhan_factory(frames, serves, name)
    swaps the primary (default OfflineDhan); expect_served overrides the harness
    sanity check of `served_by` for scenarios with outages.
    Returns (desk, notifier, dhan, yahoo).
    """
    desk_cfg = dataclasses.replace(
        small_cfg(), context=(ContextCfg("INDIAVIX", "India VIX"),) if with_vix else ()
    )
    d, notifier = new_desk(app_cfg, db, desk_cfg=desk_cfg)
    frames = make_frames(with_vix)
    yahoo = (yahoo_cls or OfflineYahoo)(frames)
    dhan = None if serves is None else (dhan_factory or OfflineDhan)(frames, serves, primary_name)
    d.source = CompositeBars(dhan, yahoo)
    d.fine_minutes = BAR_MIN
    # small_cfg says source "yahoo" and there is no adapter, so a poll would swap in a real
    # Yahoo source (network). Keep the composite we installed.
    monkeypatch.setattr(d, "_maybe_switch_source", lambda: None)
    d.poll()
    # harness sanity: the provenance is what the scenario says it is
    every_key = KEYS + (("INDIAVIX",) if with_vix else ())
    want = (
        {k: (primary_name if k in (serves or ()) else "yahoo") for k in every_key}
        if expect_served == "derive"
        else expect_served
    )
    assert d.source.served_by == want, f"harness: served_by is {d.source.served_by}, want {want}"
    assert not d.signals, "harness: a stray signal fired during the poll"
    return d, notifier, dhan, yahoo


def raise_alert(d: Desk, key: str) -> dict:
    """Raise a long alert on `key` through Desk._raise_signal and return the pending alert."""
    t = d.trackers[key]
    close = float(t.bars["close"].iloc[-1])
    # a distinct bar per call: since OF-5 the desk never alerts the same bar twice
    bar_ts = t.bars.index[-1].to_pydatetime() + dt.timedelta(seconds=len(d.archive))
    v = Verdict("long", "test", bar_ts, close * 0.995, close, 60.0, ["test"], True)
    d._raise_signal(t, t.slots[0], v, now_ist())
    alerts = [s for s in d.signals.values() if s["series_key"] == key]
    assert len(alerts) == 1, f"harness: no alert was raised for {key} ({t.last_error})"
    return alerts[0]


def clear_alert(d: Desk, alert: dict) -> None:
    assert d.acknowledge(alert["id"], "SKIP")["accepted"]


def api_client(desk: Desk):
    """The real FastAPI app over a stub engine that only carries the desk (no network)."""
    with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    engine = MagicMock()
    engine.desk = desk
    return TestClient(create_app(engine, "test-token"))


def snapshot_now(d: Desk, now=None) -> dict:
    now = now or now_ist()
    d._rebuild_snapshot(now, OpenClock().current_window(now))
    return d.snapshot()


def rows_by_key(snap: dict) -> dict[str, dict]:
    return {r["key"]: r for r in snap["indices"] + snap["stocks"]}


# (id, primary name, keys Dhan serves | None, header delayed, delayed_keys)
WORLDS = [
    pytest.param("dhan", set(KEYS), False, [], id="dhan-served-everything"),
    pytest.param("dhan-ws", set(KEYS), False, [], id="dhan-ws-served-everything"),
    pytest.param("dhan", {"SBIN"}, True, ["BANKNIFTY"], id="yahoo-served-the-index"),
    pytest.param("dhan", {"BANKNIFTY"}, True, ["SBIN"], id="yahoo-served-the-stock"),
    pytest.param("dhan", set(), True, ["BANKNIFTY", "SBIN"], id="dhan-had-no-bars-at-all"),
    pytest.param("dhan", None, True, ["BANKNIFTY", "SBIN"], id="yahoo-only-no-primary"),
]


def assert_yahoo_note(data: dict) -> None:
    note = data["delay_note"]
    assert "yahoo" in note.lower() and "15 minutes" in note and "behind" in note, note


def assert_live_note(data: dict) -> None:
    note = data["delay_note"]
    assert "live exchange feed" in note, note
    assert "15" not in note and "behind" not in note and "yahoo" not in note.lower(), note


# ══ AC1, clause 1: the alert ══════════════════════════════════════════════════════════
class TestAlertIsLabelledByTheSourceThatServedIt:
    def test_alert_on_the_yahoo_served_instrument_is_delayed_with_the_15_minute_note(
        self, app_cfg, db, monkeypatch
    ):
        # Dhan primary, but Dhan returned no bars for BANKNIFTY so Yahoo served it
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        assert d.source.served_by["BANKNIFTY"] == "yahoo"
        data = raise_alert(d, "BANKNIFTY")["data"]
        assert data["source"] == "yahoo"
        assert data["delayed"] is True, "a Yahoo-served alert must not be labelled live"
        assert_yahoo_note(data)

    def test_alert_on_the_dhan_served_instrument_is_live_even_beside_a_yahoo_served_one(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        assert d.source.served_by["SBIN"] == "dhan"
        data = raise_alert(d, "SBIN")["data"]
        assert data["source"] == "dhan"
        assert data["delayed"] is False
        assert_live_note(data)

    @pytest.mark.parametrize("yahoo_key, dhan_key", [("BANKNIFTY", "SBIN"), ("SBIN", "BANKNIFTY")])
    def test_each_alert_in_one_desk_carries_its_own_label_whichever_series_fell_back(
        self, app_cfg, db, monkeypatch, yahoo_key, dhan_key
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={dhan_key})
        flags = {k: raise_alert(d, k)["data"]["delayed"] for k in KEYS}
        assert flags == {yahoo_key: True, dhan_key: False}

    @pytest.mark.parametrize("primary_name", ["dhan", "dhan-ws"])
    def test_dhan_served_alerts_are_live_whichever_dhan_source_served_them(
        self, app_cfg, db, monkeypatch, primary_name
    ):
        d, _, _, yahoo = make_desk(
            app_cfg, db, monkeypatch, serves=set(KEYS), primary_name=primary_name
        )
        assert yahoo.requested == [], "harness: Yahoo must not be asked when Dhan served all"
        for key in KEYS:
            data = raise_alert(d, key)["data"]
            assert data["source"] == primary_name
            assert data["delayed"] is False
            assert_live_note(data)

    def test_a_yahoo_only_desk_labels_every_alert_delayed(self, app_cfg, db, monkeypatch):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves=None)  # no primary at all
        assert d.source.primary is None
        for key in KEYS:
            alert = raise_alert(d, key)
            assert alert["data"]["source"] == "yahoo"
            assert alert["data"]["delayed"] is True
            assert_yahoo_note(alert["data"])
            clear_alert(d, alert)

    def test_the_label_follows_the_provider_poll_by_poll_when_dhan_drops_and_recovers(
        self, app_cfg, db, monkeypatch
    ):
        d, _, dhan, _ = make_desk(app_cfg, db, monkeypatch, serves=set(KEYS))
        phases = [
            # (what happens to Dhan before the poll, expected provenance, delayed, header keys)
            ("steady", "dhan", False, []),
            ("dhan drops BANKNIFTY", "yahoo", True, ["BANKNIFTY"]),
            ("dhan recovers BANKNIFTY", "dhan", False, []),
        ]
        for step, provider, delayed, header_keys in phases:
            if step == "dhan drops BANKNIFTY":
                dhan.serves.discard("BANKNIFTY")
                d.poll()
            elif step == "dhan recovers BANKNIFTY":
                dhan.serves.add("BANKNIFTY")
                d.poll()
            assert d.source.served_by["BANKNIFTY"] == provider, f"harness: {step}"
            alert = raise_alert(d, "BANKNIFTY")
            assert alert["data"]["source"] == provider
            assert alert["data"]["delayed"] is delayed, f"{step}: the label must not be sticky"
            assert snapshot_now(d)["source"]["delayed_keys"] == header_keys, step
            clear_alert(d, alert)

    def test_a_pending_alert_keeps_its_delay_label_after_a_desk_restart(
        self, app_cfg, db, monkeypatch
    ):
        d, notifier, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        ids = {k: raise_alert(d, k)["id"] for k in KEYS}
        restarted, _ = new_desk(app_cfg, db, notifier)  # recovers pending alerts from the DB
        assert restarted.signals[ids["BANKNIFTY"]]["data"]["delayed"] is True
        assert restarted.signals[ids["SBIN"]]["data"]["delayed"] is False
        assert_yahoo_note(restarted.signals[ids["BANKNIFTY"]]["data"])
        assert_live_note(restarted.signals[ids["SBIN"]]["data"])


class TestTelegramReadsTheSameFlag:
    """US-003's formatter will read alert["data"]["delayed"] only. The notification handed to
    the notifier carries that alert as its payload, so the tag cannot diverge from the label."""

    @pytest.mark.parametrize("dhan_serves, expected", [({"SBIN"}, True), (set(KEYS), False)])
    def test_the_notification_payload_carries_the_alert_delay_flag(
        self, app_cfg, db, monkeypatch, dhan_serves, expected
    ):
        d, notifier, _, _ = make_desk(app_cfg, db, monkeypatch, serves=dhan_serves)
        alert = raise_alert(d, "BANKNIFTY")
        sent = notifier.sent[-1]
        assert sent.level.value == "SIGNAL" and sent.signal_id == alert["id"]
        assert sent.payload["data"]["delayed"] is expected
        assert sent.payload["data"]["delayed"] is alert["data"]["delayed"]
        assert sent.payload["data"]["delay_note"] == alert["data"]["delay_note"]

    def test_the_flag_in_the_payload_is_json_serialisable_and_survives_the_round_trip(
        self, app_cfg, db, monkeypatch
    ):
        d, notifier, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        raise_alert(d, "BANKNIFTY")
        wire = json.loads(json.dumps(notifier.sent[-1].payload["data"]))
        assert wire["delayed"] is True and "15 minutes" in wire["delay_note"]


# ══ AC1, clause 3 (header + one rule): the Desk snapshot ═════════════════════════════
class TestDeskHeaderBadge:
    @pytest.mark.parametrize("primary_name, dhan_serves, delayed, delayed_keys", WORLDS)
    def test_header_is_delayed_iff_any_tracker_is_yahoo_served_and_lists_exactly_those_keys(
        self, app_cfg, db, monkeypatch, primary_name, dhan_serves, delayed, delayed_keys
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, dhan_serves, primary_name)
        header = d.snapshot()["source"]
        assert header["delayed"] is delayed
        assert header["delayed_keys"] == delayed_keys  # a sorted list, not a set or tuple
        # the badge is served to the browser as JSON
        assert json.loads(json.dumps(header))["delayed_keys"] == delayed_keys

    @pytest.mark.parametrize("primary_name, dhan_serves, delayed, delayed_keys", WORLDS)
    def test_header_keeps_naming_the_source_while_it_flags_the_delay(
        self, app_cfg, db, monkeypatch, primary_name, dhan_serves, delayed, delayed_keys
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, dhan_serves, primary_name)
        header = d.snapshot()["source"]
        assert header["name"] == ("yahoo" if dhan_serves is None else f"{primary_name}+yahoo")
        assert header["resolution"] == "2m" and header["polls"] == 1

    def test_a_yahoo_only_desk_is_delayed_even_before_its_first_poll(self, app_cfg, db):
        d, _ = new_desk(app_cfg, db)  # the built-in source: no primary, nothing fetched yet
        assert d.source.primary is None and d.source.served_by == {}
        snap = snapshot_now(d)
        assert snap["source"]["delayed"] is True
        assert snap["source"]["delayed_keys"] == ["BANKNIFTY", "SBIN"]
        rows = rows_by_key(snap)
        assert all(r["health"]["delayed"] is True for r in rows.values())
        assert all(r["health"]["state"] == "NO DATA" for r in rows.values())


class TestTrackerRowsAndHeaderAndAlertsAgree:
    @pytest.mark.parametrize("primary_name, dhan_serves, delayed, delayed_keys", WORLDS)
    def test_each_tracker_row_reports_its_own_providers_delay_flag(
        self, app_cfg, db, monkeypatch, primary_name, dhan_serves, delayed, delayed_keys
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, dhan_serves, primary_name)
        rows = rows_by_key(d.snapshot())
        assert set(rows) == set(KEYS)
        for key in KEYS:
            health = rows[key]["health"]
            assert health["delayed"] is (key in delayed_keys), f"{key}: {health}"
            assert health["source"] == d.source.served_by[key]

    @pytest.mark.parametrize("primary_name, dhan_serves, delayed, delayed_keys", WORLDS)
    def test_alert_tracker_row_and_header_all_apply_the_same_rule(
        self, app_cfg, db, monkeypatch, primary_name, dhan_serves, delayed, delayed_keys
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, dhan_serves, primary_name)
        for key in KEYS:
            raise_alert(d, key)
        snap = snapshot_now(d)
        rows = rows_by_key(snap)
        for key in KEYS:
            alert_flag = rows[key]["signal"]["data"]["delayed"]
            assert alert_flag is rows[key]["health"]["delayed"], f"{key}: alert vs health"
            assert alert_flag is (key in snap["source"]["delayed_keys"]), f"{key}: alert vs header"
        assert snap["source"]["delayed"] is any(
            rows[k]["signal"]["data"]["delayed"] for k in KEYS
        ), "header badge must be on iff any alert / tracker is delayed"
        assert snap["source"]["delayed"] is delayed

    def test_the_pending_alert_list_in_the_snapshot_carries_the_labels(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        for key in KEYS:
            raise_alert(d, key)
        pending = {s["series_key"]: s["data"]["delayed"] for s in snapshot_now(d)["signals"]}
        assert pending == {"BANKNIFTY": True, "SBIN": False}


# ══ tracker health must use the per-tracker flag ════════════════════════════════════
class TestTrackerHealthUsesThePerTrackerFlag:
    """Health allows ~15 extra minutes of bar age only for a delayed feed. The allowance has to
    come from the tracker's own provider, not from the desk-wide flag (which is now "any")."""

    @staticmethod
    def _at(d: Desk, minutes_after_close: float):
        last_close = d.trackers["SBIN"].bars.index[-1] + pd.Timedelta(minutes=BAR_MIN)
        assert d.trackers["BANKNIFTY"].bars.index[-1] == d.trackers["SBIN"].bars.index[-1]
        return last_close + pd.Timedelta(minutes=minutes_after_close)

    def test_a_15_minute_old_bar_is_ok_for_the_yahoo_served_tracker_and_stale_for_dhan(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        # both series' last bars closed exactly 15 minutes before `now`
        rows = rows_by_key(snapshot_now(d, self._at(d, 15)))
        yahoo_row, dhan_row = rows["BANKNIFTY"]["health"], rows["SBIN"]["health"]
        assert yahoo_row["state"] == "OK", f"Yahoo is always ~15 min behind: {yahoo_row}"
        assert "delayed" in yahoo_row["note"]
        assert dhan_row["state"] == "STALE", f"a live Dhan feed 15 min behind is stale: {dhan_row}"
        assert rows["BANKNIFTY"]["last"]["age_sec"] == pytest.approx(15 * 60)

    @pytest.mark.parametrize(
        "dhan_serves, minutes_after_close, state",
        [
            ({"SBIN"}, 15, "OK"),  # Yahoo: 15 min is normal
            ({"SBIN"}, 30, "LAGGING"),  # Yahoo: beyond its 15 min allowance, but not dead
            ({"SBIN"}, 60, "STALE"),  # Yahoo: the allowance is not a bypass
            (set(KEYS), 15, "STALE"),  # Dhan: 15 min is a dead feed
            (set(KEYS), 1, "OK"),  # Dhan: a bar that closed a minute ago is live
        ],
        ids=[
            "yahoo-15m-ok",
            "yahoo-30m-lagging",
            "yahoo-60m-stale",
            "dhan-15m-stale",
            "dhan-1m-ok",
        ],
    )
    def test_the_index_trackers_health_state_follows_its_own_providers_budget(
        self, app_cfg, db, monkeypatch, dhan_serves, minutes_after_close, state
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves=dhan_serves)
        row = rows_by_key(snapshot_now(d, self._at(d, minutes_after_close)))["BANKNIFTY"]
        assert row["health"]["state"] == state, row["health"]

    def test_a_healthy_yahoo_tracker_says_so_in_its_health_note(self, app_cfg, db, monkeypatch):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        health = rows_by_key(snapshot_now(d, self._at(d, 15)))["BANKNIFTY"]["health"]
        assert health["delayed"] is True and health["source"] == "yahoo"
        assert "15" in health["note"] and "delayed" in health["note"]

    def test_a_healthy_dhan_tracker_next_to_a_yahoo_one_still_reads_live(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        health = rows_by_key(snapshot_now(d, self._at(d, 1)))["SBIN"]["health"]
        assert health["state"] == "OK" and health["delayed"] is False
        assert health["note"] == "live"


# ══ mode B: outages ═══════════════════════════════════════════════════════════════════
class TestYahooOutageNextToADhanOutage:
    """Dhan having no bars sends a series to Yahoo; Yahoo can be down as well. The label must
    follow the bars the tracker actually holds, and an outage must never launder delayed bars
    into a "live" label or crash the poll."""

    @staticmethod
    def _yahoo_down(monkeypatch):
        def boom(*_a, **_k):
            raise ConnectionError("yahoo is down")

        monkeypatch.setattr("yfinance.download", boom)

    def test_neither_provider_has_bars_the_poll_survives_and_nothing_is_called_delayed(
        self, app_cfg, db, monkeypatch
    ):
        self._yahoo_down(monkeypatch)
        d, _, _, _ = make_desk(
            app_cfg,
            db,
            monkeypatch,
            serves=set(),
            yahoo_cls=lambda _frames: YahooBars(),  # the real class; its download() raises
            expect_served={},
        )
        snap = d.snapshot()
        rows = rows_by_key(snap)
        assert all(r["health"]["state"] == "NO DATA" for r in rows.values())
        assert all(r["health"]["delayed"] is False for r in rows.values())
        assert snap["source"]["delayed"] is False and snap["source"]["delayed_keys"] == []
        assert "ConnectionError" in snap["source"]["last_error"]
        assert "yahoo is down" in snap["source"]["last_error"]
        assert d.signals == {}

    def test_yahoo_outage_leaves_the_dhan_served_instrument_live_and_the_other_without_data(
        self, app_cfg, db, monkeypatch
    ):
        self._yahoo_down(monkeypatch)
        d, _, _, _ = make_desk(
            app_cfg,
            db,
            monkeypatch,
            serves={"SBIN"},
            yahoo_cls=lambda _frames: YahooBars(),
            expect_served={"SBIN": "dhan"},
        )
        snap = snapshot_now(d)
        rows = rows_by_key(snap)
        assert rows["BANKNIFTY"]["health"]["state"] == "NO DATA"
        assert rows["BANKNIFTY"]["health"]["delayed"] is False
        assert rows["SBIN"]["health"]["delayed"] is False
        assert snap["source"]["delayed"] is False and snap["source"]["delayed_keys"] == []
        assert "yahoo is down" in snap["source"]["last_error"]
        data = raise_alert(d, "SBIN")["data"]
        assert data["source"] == "dhan" and data["delayed"] is False
        assert_live_note(data)

    def test_a_yahoo_outage_does_not_launder_held_yahoo_bars_into_a_live_label(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, yahoo = make_desk(app_cfg, db, monkeypatch, {"SBIN"}, yahoo_cls=FlakyYahoo)
        held = len(d.trackers["BANKNIFTY"].bars)
        yahoo.down = True
        d.poll()  # BANKNIFTY: Dhan has none, Yahoo is down -> nothing new arrives
        assert len(d.trackers["BANKNIFTY"].bars) == held, "harness: the outage added bars"
        assert "yahoo is down" in (d.snapshot()["source"]["last_error"] or "")
        alert = raise_alert(d, "BANKNIFTY")  # built on the Yahoo bars the tracker still holds
        assert alert["data"]["source"] == "yahoo" and alert["data"]["delayed"] is True
        assert_yahoo_note(alert["data"])
        snap = snapshot_now(d)
        assert snap["source"]["delayed"] is True and snap["source"]["delayed_keys"] == ["BANKNIFTY"]
        assert rows_by_key(snap)["BANKNIFTY"]["health"]["delayed"] is True

    def test_both_providers_dropping_a_dhan_series_keeps_dhan_bars_live_but_health_goes_stale(
        self, app_cfg, db, monkeypatch
    ):
        d, _, dhan, yahoo = make_desk(app_cfg, db, monkeypatch, set(KEYS), yahoo_cls=FlakyYahoo)
        dhan.serves.discard("BANKNIFTY")
        yahoo.down = True
        d.poll()  # the series is now in nobody's hands: the held Dhan bars just age
        assert d.source.served_by["BANKNIFTY"] == "dhan"
        alert = raise_alert(d, "BANKNIFTY")
        assert alert["data"]["source"] == "dhan" and alert["data"]["delayed"] is False
        last_close = d.trackers["BANKNIFTY"].bars.index[-1] + pd.Timedelta(minutes=BAR_MIN)
        rows = rows_by_key(snapshot_now(d, last_close + pd.Timedelta(minutes=60)))
        # a live Dhan series with an hour-old bar is a dead feed, whatever the label says
        assert rows["BANKNIFTY"]["health"]["state"] == "STALE"
        assert rows["BANKNIFTY"]["health"]["delayed"] is False


# ══ mode B: market-closed health branch ═══════════════════════════════════════════════
class TestMarketClosedHealth:
    """CLOSED beats the age rules, but must not erase which provider a tracker is on."""

    @pytest.mark.parametrize("window_kind", ["shut-session", "no-window-holiday"])
    @pytest.mark.parametrize("minutes_after_close", [5, 600])
    def test_closed_market_reads_closed_for_both_providers_and_keeps_each_trackers_label(
        self, app_cfg, db, monkeypatch, window_kind, minutes_after_close
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        last_close = d.trackers["SBIN"].bars.index[-1] + pd.Timedelta(minutes=BAR_MIN)
        now = last_close + pd.Timedelta(minutes=minutes_after_close)
        window = ClosedWindow(now) if window_kind == "shut-session" else None
        d._rebuild_snapshot(now, window)
        snap = d.snapshot()
        assert snap["session"]["open"] is False
        rows = rows_by_key(snap)
        for key, delayed in {"BANKNIFTY": True, "SBIN": False}.items():
            health = rows[key]["health"]
            assert health["state"] == "CLOSED", f"{key}: {health}"
            assert health["note"].startswith("market closed")
            assert health["delayed"] is delayed, f"{key}: CLOSED must not erase the label"
        assert snap["source"]["delayed"] is True
        assert snap["source"]["delayed_keys"] == ["BANKNIFTY"]

    def test_an_alert_is_still_labelled_by_its_provider_while_the_market_is_closed(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        assert raise_alert(d, "BANKNIFTY")["data"]["delayed"] is True
        d._rebuild_snapshot(now_ist(), ClosedWindow(now_ist()))
        rows = rows_by_key(d.snapshot())
        assert rows["BANKNIFTY"]["signal"]["data"]["delayed"] is True
        assert rows["BANKNIFTY"]["health"]["state"] == "CLOSED"


# ══ mode B: context-strip series are not trackers ═════════════════════════════════════
class TestContextSeriesNeverLeakIntoTheHeader:
    """INDIAVIX/USDINR come from Yahoo even with a Dhan plan. The header badge and
    delayed_keys speak about TRACKER keys only (Manager decision), so a Yahoo-served context
    series must not flag the desk, and a Dhan-served one must not hide a delayed tracker."""

    @pytest.mark.parametrize(
        "serves, vix_source, delayed, delayed_keys",
        [
            (set(KEYS), "yahoo", False, []),  # trackers live, VIX on Yahoo: header stays live
            ({"INDIAVIX"}, "dhan", True, ["BANKNIFTY", "SBIN"]),  # trackers on Yahoo, VIX on Dhan
            (None, "yahoo", True, ["BANKNIFTY", "SBIN"]),  # Yahoo-only desk
            ({"SBIN", "INDIAVIX"}, "dhan", True, ["BANKNIFTY"]),
        ],
        ids=["live-trackers-yahoo-vix", "yahoo-trackers-dhan-vix", "yahoo-only", "mixed"],
    )
    def test_delayed_keys_lists_tracker_keys_only(
        self, app_cfg, db, monkeypatch, serves, vix_source, delayed, delayed_keys
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves, with_vix=True)
        assert d.source.served_by["INDIAVIX"] == vix_source, "harness"
        snap = d.snapshot()
        assert snap["source"]["delayed"] is delayed
        assert snap["source"]["delayed_keys"] == delayed_keys
        assert "INDIAVIX" not in snap["source"]["delayed_keys"]
        assert {r["key"] for r in snap["indices"] + snap["stocks"]} == set(KEYS)

    def test_the_context_row_still_reports_which_provider_served_it(self, app_cfg, db, monkeypatch):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, set(KEYS), with_vix=True)
        (vix,) = [r for r in d.snapshot()["context"] if r["key"] == "INDIAVIX"]
        assert vix["source"] == "yahoo" and vix["last"] is not None
        assert d.snapshot()["source"]["delayed"] is False  # ...without flagging the desk

    def test_alerts_on_live_trackers_stay_live_while_the_vix_strip_is_on_yahoo(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, set(KEYS), with_vix=True)
        for key in KEYS:
            data = raise_alert(d, key)["data"]
            assert data["delayed"] is False
            assert_live_note(data)


# ══ mode B: the SIGNAL notification title ═════════════════════════════════════════════
MARKER = "(delayed ~15m) "
BLOCKED = "[BLOCKED by risk] "


class TestSignalTitleMarker:
    def test_a_delayed_title_differs_from_the_live_title_only_by_the_leading_marker(
        self, app_cfg, db, monkeypatch
    ):
        d, notifier, dhan, _ = make_desk(app_cfg, db, monkeypatch, set(KEYS))
        raise_alert(d, "SBIN")
        live_title = notifier.sent[-1].title
        clear_alert(d, next(iter(d.signals.values())))
        dhan.serves.discard("SBIN")
        d.poll()  # SBIN now served by Yahoo, identical bars, identical verdict
        alert = raise_alert(d, "SBIN")
        assert alert["data"]["delayed"] is True
        assert MARKER not in live_title and "delayed" not in live_title
        assert notifier.sent[-1].title == MARKER + live_title
        assert notifier.sent[-1].title.count("(delayed ~15m)") == 1

    @pytest.mark.parametrize("key", ["BANKNIFTY", "SBIN"])
    def test_both_stock_and_index_alerts_lead_with_the_marker_only_when_yahoo_served(
        self, app_cfg, db, monkeypatch, key
    ):
        other = "SBIN" if key == "BANKNIFTY" else "BANKNIFTY"
        d, notifier, _, _ = make_desk(app_cfg, db, monkeypatch, serves={other})
        raise_alert(d, key)
        assert notifier.sent[-1].title.startswith(MARKER)
        raise_alert(d, other)
        assert not notifier.sent[-1].title.startswith(MARKER)
        assert "delayed" not in notifier.sent[-1].title

    def test_a_halted_governor_changes_nothing_and_delayed_still_leads(
        self, app_cfg, db, monkeypatch
    ):
        """Owner feedback OF-5: no risk governor on alerts, so a halt adds no BLOCKED."""
        d, notifier, dhan, _ = make_desk(app_cfg, db, monkeypatch, set(KEYS))
        raise_alert(d, "SBIN")
        base = notifier.sent[-1].title  # live
        clear_alert(d, next(iter(d.signals.values())))
        d.governor.manual_panic_halt()
        halted_live = raise_alert(d, "SBIN")
        assert halted_live["risk_allowed"] is True
        assert notifier.sent[-1].title == base
        clear_alert(d, halted_live)
        dhan.serves.discard("SBIN")
        d.poll()
        halted_delayed = raise_alert(d, "SBIN")
        assert halted_delayed["risk_allowed"] is True and halted_delayed["data"]["delayed"]
        title = notifier.sent[-1].title
        assert title == MARKER + base
        assert BLOCKED.strip() not in title and title.count("(delayed ~15m)") == 1

    def test_title_marker_changes_nothing_else_in_the_notification_or_the_alert(
        self, app_cfg, db, monkeypatch
    ):
        d, notifier, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        alert = raise_alert(d, "BANKNIFTY")  # Yahoo-served
        sent = notifier.sent[-1]
        assert sent.title.startswith(MARKER)
        assert sent.level.value == "SIGNAL" and sent.signal_id == alert["id"]
        assert sent.ttl_seconds == 10 * 60  # signal_valid_minutes of small_cfg
        assert sent.payload is alert
        assert sent.body == alert["text"], "the body is the alert text, no marker added"
        assert "(delayed ~15m)" not in alert["text"] and "(delayed ~15m)" not in sent.body
        assert "(delayed ~15m)" not in json.dumps(alert, default=str), "title-only change"
        assert alert["data"]["delay_note"] == "Yahoo bars run ~15 minutes behind the exchange"

    def test_a_halted_governor_adds_no_risk_line_to_a_delayed_body(self, app_cfg, db, monkeypatch):
        d, notifier, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        d.governor.manual_panic_halt()
        alert = raise_alert(d, "BANKNIFTY")
        sent = notifier.sent[-1]
        assert sent.title.startswith(MARKER)
        assert sent.body == alert["text"]
        assert "Risk governor" not in sent.body


# ══ mode B: end to end through the HTTP API and the UI helper ═════════════════════════
class TestEndToEnd:
    def test_get_api_desk_serves_every_label_the_ui_reads(self, app_cfg, db, monkeypatch):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        for key in KEYS:
            raise_alert(d, key)
        snapshot_now(d)
        body = api_client(d).get("/api/desk").json()
        assert body["source"]["delayed"] is True
        assert body["source"]["delayed_keys"] == ["BANKNIFTY"]
        rows = rows_by_key(body)
        assert rows["BANKNIFTY"]["health"]["delayed"] is True
        assert rows["SBIN"]["health"]["delayed"] is False
        assert rows["BANKNIFTY"]["signal"]["data"]["delayed"] is True
        assert rows["SBIN"]["signal"]["data"]["delayed"] is False
        assert "15 minutes" in rows["BANKNIFTY"]["signal"]["data"]["delay_note"]

    def test_the_sse_encoding_of_the_desk_snapshot_keeps_the_flags_as_json_booleans(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves={"SBIN"})
        wire = json.loads(json.dumps({"state": {"desk": snapshot_now(d)}}, default=str))
        header = wire["state"]["desk"]["source"]
        assert header["delayed"] is True and header["delayed_keys"] == ["BANKNIFTY"]


APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; UI helper not exercised"
)


def run_app_js(lines: list[str], expression: str, payload) -> object:
    """Evaluate `expression` (JS, `payload` available as `P`) in node after loading the named
    one-line helpers from src/onelot/ui/app.js. No browser, no DOM."""
    js = APP_JS.read_text(encoding="utf-8")
    helpers = []
    for name in lines:
        m = re.search(rf"^const {name} = .*;$", js, re.M)
        assert m, f"`const {name} = ...;` not found as one line in {APP_JS}"
        helpers.append(m.group(0))
    script = "\n".join([*helpers, "const P = JSON.parse(process.argv[1]);", expression])
    done = subprocess.run(
        ["node", "-e", script, json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
class TestHeaderTextFromTheRealSnapshot:
    """The very `snapshot()["source"]` a polled Desk produces, rendered by app.js's own
    delayNote(): what the header badge and the desk-source line will say."""

    @pytest.mark.parametrize(
        "serves, text",
        [
            (set(KEYS), "live"),
            ({"SBIN"}, "delayed ~15 min: BANKNIFTY"),
            ({"BANKNIFTY"}, "delayed ~15 min: SBIN"),
            (set(), "delayed ~15 min: BANKNIFTY, SBIN"),
            (None, "delayed ~15 min: BANKNIFTY, SBIN"),
        ],
        ids=["live", "index-on-yahoo", "stock-on-yahoo", "dhan-empty", "yahoo-only"],
    )
    def test_the_header_text_names_exactly_the_delayed_instruments(
        self, app_cfg, db, monkeypatch, serves, text
    ):
        d, _, _, _ = make_desk(app_cfg, db, monkeypatch, serves)
        header = json.loads(json.dumps(d.snapshot()["source"], default=str))
        assert (
            run_app_js(["delayNote"], "console.log(JSON.stringify(delayNote(P)))", header) == text
        )


# ══ mode B: the real DhanBars class, not a stand-in ═══════════════════════════════════
class TestWithTheRealDhanBarsClass:
    """Everything above uses a fake primary. Here the primary is the real DhanBars (name
    "dhan", `_clean`, `can_serve`, error handling) over a fake Dhan adapter, so the claim
    "Dhan returned no bars so Yahoo served it" is exercised through production code."""

    @staticmethod
    def _real_dhan(fail=()):
        return lambda frames, serves, _name: DhanBars(
            FakeDhanAdapter(frames, serves, fail), pause_s=0.0
        )

    def test_no_bars_from_dhan_for_one_series_sends_it_to_yahoo_and_labels_it_delayed(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(
            app_cfg, db, monkeypatch, serves={"SBIN"}, dhan_factory=self._real_dhan()
        )
        assert d.source.name == "dhan+yahoo" and d.source.served_by["BANKNIFTY"] == "yahoo"
        banknifty, sbin = raise_alert(d, "BANKNIFTY")["data"], raise_alert(d, "SBIN")["data"]
        assert (banknifty["source"], banknifty["delayed"]) == ("yahoo", True)
        assert (sbin["source"], sbin["delayed"]) == ("dhan", False)
        assert_yahoo_note(banknifty)
        assert_live_note(sbin)
        header = snapshot_now(d)["source"]
        assert header["delayed"] is True and header["delayed_keys"] == ["BANKNIFTY"]

    def test_a_dhan_api_error_on_one_series_falls_back_and_is_labelled_delayed(
        self, app_cfg, db, monkeypatch
    ):
        d, _, _, _ = make_desk(
            app_cfg,
            db,
            monkeypatch,
            serves=set(KEYS),
            dhan_factory=self._real_dhan(fail={"BANKNIFTY"}),
            expect_served={"SBIN": "dhan", "BANKNIFTY": "yahoo"},
        )
        data = raise_alert(d, "BANKNIFTY")["data"]
        assert (data["source"], data["delayed"]) == ("yahoo", True)
        header = snapshot_now(d)["source"]
        assert header["delayed_keys"] == ["BANKNIFTY"]
        assert "BANKNIFTY" in header["last_error"] and "DH-904" in header["last_error"]
        assert rows_by_key(d.snapshot())["SBIN"]["health"]["delayed"] is False


# ══ mode B: every combination of who served what ══════════════════════════════════════
ALL_SERVES = [
    frozenset(c)
    for n in range(4)
    for c in itertools.combinations(("BANKNIFTY", "SBIN", "INDIAVIX"), n)
]


@pytest.mark.parametrize(
    "serves",
    [*ALL_SERVES, None],
    ids=[("dhan:" + "+".join(sorted(c)) if c else "dhan:none") for c in ALL_SERVES]
    + ["yahoo-only"],
)
def test_every_combination_of_dhan_and_yahoo_served_series_is_labelled_consistently(
    app_cfg, db, monkeypatch, serves
):
    """Equivalence classes of provenance: for each subset of {two trackers, one context series}
    served by Dhan (the rest by Yahoo), the alert flag, the SIGNAL title marker, the tracker
    health flag and the header badge all agree with the independent oracle "a tracker is
    delayed iff Yahoo served it"."""
    d, notifier, _, _ = make_desk(
        app_cfg, db, monkeypatch, None if serves is None else set(serves), with_vix=True
    )
    delayed_trackers = sorted(k for k in KEYS if serves is None or k not in serves)
    for key in KEYS:
        expected = key in delayed_trackers
        alert = raise_alert(d, key)
        assert alert["data"]["delayed"] is expected, key
        assert alert["data"]["source"] == ("yahoo" if expected else "dhan"), key
        assert notifier.sent[-1].title.startswith(MARKER) is expected, key
    snap = snapshot_now(d)
    assert snap["source"]["delayed"] is bool(delayed_trackers)
    assert snap["source"]["delayed_keys"] == delayed_trackers
    for key, row in rows_by_key(snap).items():
        expected = key in delayed_trackers
        assert row["health"]["delayed"] is expected, key
        assert row["signal"]["data"]["delayed"] is expected, key
