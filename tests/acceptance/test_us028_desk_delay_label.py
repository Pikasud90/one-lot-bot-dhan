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

import datetime as dt
import json
import socket

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST, now_ist
from onelot.config import AppConfig
from onelot.desk import Desk
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.data import COLUMNS, CompositeBars, YahooBars
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


def make_frames() -> dict[str, pd.DataFrame]:
    end = feed_end()
    return {
        "BANKNIFTY": session_bars(54000.0, seed=11, end=end),
        "SBIN": session_bars(950.0, seed=12, end=end),
    }


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


class OfflineYahoo(YahooBars):
    """The real YahooBars class (name "yahoo", delayed True) with the network cut off."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        super().__init__("2m")
        self.frames = frames
        self.requested: list[list[str]] = []

    def fetch(self, specs, days, since=None):
        self.requested.append(sorted(specs))
        return {k: self.frames[k] for k in specs if k in self.frames}


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


def new_desk(app_cfg, db, notifier=None) -> tuple[Desk, FakeNotifier]:
    repo = Repo(db)
    gov = RiskGovernor(
        app_cfg.risk, app_cfg.account.capital_inr, app_cfg.mode, now_ist().date().isoformat(), repo
    )
    notifier = notifier or FakeNotifier()
    desk = Desk(db.engine, app_cfg, notifier, gov, OpenClock(), repo=repo, desk_cfg=small_cfg())
    return desk, notifier


def make_desk(app_cfg, db, monkeypatch, serves, primary_name="dhan"):
    """A polled Desk on a CompositeBars.

    serves is None  -> no Dhan primary at all (a Yahoo-only desk, as without a Dhan plan);
    serves is a set -> a Dhan primary that has bars for those keys and none for the others,
                       so the Yahoo fallback serves the rest.
    Returns (desk, notifier, dhan, yahoo).
    """
    d, notifier = new_desk(app_cfg, db)
    frames = make_frames()
    yahoo = OfflineYahoo(frames)
    dhan = None if serves is None else OfflineDhan(frames, serves, primary_name)
    d.source = CompositeBars(dhan, yahoo)
    d.fine_minutes = BAR_MIN
    # small_cfg says source "yahoo" and there is no adapter, so a poll would swap in a real
    # Yahoo source (network). Keep the composite we installed.
    monkeypatch.setattr(d, "_maybe_switch_source", lambda: None)
    d.poll()
    # harness sanity: the provenance is what the scenario says it is
    want = {k: (primary_name if k in (serves or ()) else "yahoo") for k in KEYS}
    assert d.source.served_by == want, f"harness: served_by is {d.source.served_by}, want {want}"
    assert not d.signals, "harness: a stray signal fired during the poll"
    return d, notifier, dhan, yahoo


def raise_alert(d: Desk, key: str) -> dict:
    """Raise a long alert on `key` through Desk._raise_signal and return the pending alert."""
    t = d.trackers[key]
    close = float(t.bars["close"].iloc[-1])
    v = Verdict(
        "long", "test", t.bars.index[-1].to_pydatetime(), close * 0.995, close, 60.0, ["test"], True
    )
    d._raise_signal(t, t.slots[0], v, now_ist())
    alerts = [s for s in d.signals.values() if s["series_key"] == key]
    assert len(alerts) == 1, f"harness: no alert was raised for {key} ({t.last_error})"
    return alerts[0]


def clear_alert(d: Desk, alert: dict) -> None:
    assert d.acknowledge(alert["id"], "SKIP")["accepted"]


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
