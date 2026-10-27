"""Notification fan-out — v1 defect D8 (a single fragile channel)."""

from __future__ import annotations

import datetime as dt

import pytest

from onelot.clock import IST
from onelot.notify.base import Notification, NotificationLevel
from onelot.notify.dashboard import DashboardChannel
from onelot.notify.manager import DedupWindow, Notifier
from onelot.notify.os_toast import _escape_applescript, build_os_channel


class ExplodingChannel:
    name = "exploding"
    available = True

    def send(self, n):
        raise RuntimeError("channel is on fire")


class SilentlyFailingChannel:
    name = "silent_fail"
    available = True
    last_error = "permission denied"

    def send(self, n):
        return False


@pytest.fixture()
def notifier(cfg):
    quiet = cfg.model_copy(update={
        "notify": cfg.notify.model_copy(update={"os_toast": False})})
    return Notifier(quiet, dedup_seconds=0.0)


class TestFailureIsolation:
    def test_one_channel_raising_does_not_stop_the_others(self, notifier):
        notifier.channels.append(ExplodingChannel())
        notifier.stats["exploding"] = notifier.stats["dashboard"].__class__()
        res = notifier.send(Notification("t", "b"))
        assert res["dashboard"] is True
        assert res["exploding"] is False

    def test_a_silently_failing_channel_is_recorded_not_swallowed(self, notifier):
        """The v1 failure mode: plyer no-ops without a word."""
        notifier.channels.append(SilentlyFailingChannel())
        notifier.stats["silent_fail"] = notifier.stats["dashboard"].__class__()
        notifier.send(Notification("t", "b"))
        st = notifier.status()
        entry = next(c for c in st["channels"] if c["name"] == "silent_fail")
        assert entry["failed"] == 1
        assert entry["last_error"] == "permission denied"

    def test_dashboard_channel_is_always_present_and_first(self, notifier):
        assert notifier.channels[0].name == "dashboard"

    def test_dashboard_channel_never_fails(self, notifier):
        for i in range(50):
            assert notifier.send(Notification(f"t{i}", "b"))["dashboard"]


class TestDashboardQueue:
    def test_newest_first(self):
        d = DashboardChannel()
        for i in range(5):
            d.send(Notification(f"t{i}", "b"))
        assert [n["title"] for n in d.recent()][0] == "t4"

    def test_since_returns_only_new_items(self):
        d = DashboardChannel()
        d.send(Notification("old", "b"))
        _, seq = d.since(0)
        d.send(Notification("new", "b"))
        items, seq2 = d.since(seq)
        assert [i["title"] for i in items] == ["new"]
        assert seq2 > seq

    def test_bounded_so_it_cannot_grow_without_limit(self):
        d = DashboardChannel(maxlen=10)
        for i in range(50):
            d.send(Notification(f"t{i}", "b"))
        assert len(d.recent(100)) == 10


class TestDeduplication:
    def test_duplicate_inside_the_window_is_suppressed(self):
        w = DedupWindow(seconds=30.0)
        now = dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST)
        assert w.should_send("k", now)
        assert not w.should_send("k", now + dt.timedelta(seconds=10))

    def test_it_sends_again_after_the_window(self):
        w = DedupWindow(seconds=30.0)
        now = dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST)
        w.should_send("k", now)
        assert w.should_send("k", now + dt.timedelta(seconds=31))

    def test_different_keys_are_independent(self):
        w = DedupWindow(seconds=30.0)
        now = dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST)
        assert w.should_send("a", now) and w.should_send("b", now)

    def test_signal_dedup_uses_the_signal_id(self, cfg):
        """A re-sent signal alert must not double-notify, but a NEW signal must."""
        quiet = cfg.model_copy(update={
            "notify": cfg.notify.model_copy(update={"os_toast": False})})
        n = Notifier(quiet, dedup_seconds=60.0)
        assert n.send(Notification("a", "b"), dedup_key="signal:S1")
        assert not n.send(Notification("a", "b"), dedup_key="signal:S1")
        assert n.send(Notification("a", "b"), dedup_key="signal:S2")


class TestLevels:
    def test_urgent_levels_are_marked(self):
        assert NotificationLevel.SIGNAL.is_urgent
        assert NotificationLevel.EXIT.is_urgent
        assert NotificationLevel.CRITICAL.is_urgent
        assert not NotificationLevel.INFO.is_urgent


class TestAppleScriptEscaping:
    """An unescaped quote or newline makes osascript reject the whole script,
    which would silently kill the alert at the worst moment."""

    def test_quotes_are_escaped(self):
        assert '\\"' in _escape_applescript('say "hi"')

    def test_newlines_become_literal(self):
        assert "\\n" in _escape_applescript("line1\nline2")
        assert "\n" not in _escape_applescript("line1\nline2")

    def test_backslashes_are_escaped(self):
        assert _escape_applescript("a\\b") == "a\\\\b"

    def test_a_real_alert_body_survives_escaping(self):
        body = ('BUY BANKNIFTY 54000 CE x2\nEntry ~318.45 "ask 318.00"\n'
                'Target 392.00 | Stop 289.00')
        out = _escape_applescript(body)
        assert "\n" not in out and out.count('\\"') == 2


class TestPlatformChannel:
    def test_a_channel_is_selected_for_this_platform(self):
        ch = build_os_channel()
        assert ch is None or hasattr(ch, "send")

    def test_channel_reports_availability_honestly(self):
        ch = build_os_channel()
        if ch is not None:
            assert isinstance(ch.available, bool)


class TestSelfTest:
    def test_self_test_reaches_the_dashboard(self, notifier):
        assert notifier.self_test()["dashboard"] is True

    def test_status_is_serialisable(self, notifier):
        import json
        json.dumps(notifier.status())
