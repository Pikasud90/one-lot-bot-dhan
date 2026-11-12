"""US-067, the lead's rulings after the review (reviewer 1): a delivery alert reaches Telegram
as its own text, through the real `TelegramChannel` and outbox with a fake HTTP transport.

Before the fix the payload named `series_key`, so the channel took it for an intraday desk
alert and printed "Entry NA, Stop NA" with no quantity and no evidence line, and an exit read
like an entry. No network: the transport is `httpx.MockTransport`.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from onelot.config import NotifyCfg
from onelot.notify.telegram_channel import TelegramChannel

from .test_telegram_outbox import T0, Rig, ok

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "delivery"))

from test_us067_tester_desk import EVID, L, World, at  # noqa: E402


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path / "t.db", lambda request: ok())
    yield r
    r.outbox.stop()
    r.db.dispose()


@pytest.fixture
def trades(tmp_path):
    """A desk whose SBIN has an entry alert (L3) and, later, an exit alert (STOP on L6)."""
    w = World(tmp_path)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    for when in (at(L[4], "09:20"), at(L[4], "15:45"), at(L[5], "15:45"), at(L[6], "15:45")):
        w.poll(when)
    return w


def sent_texts(rig: Rig) -> list[str]:
    return [json.loads(r.content)["text"] for r in rig.requests]


@pytest.mark.parametrize("which", ["entry", "exit"])
def test_a_delivery_alert_reaches_telegram_as_its_own_text(rig, trades, which):
    t = trades.trades()["SBIN"]
    assert t.exit_reason == "STOP"
    n = trades.desk.notification(t, which, at(L[6], "15:45"))
    rig.clock.now = T0
    channel = TelegramChannel(rig.client, rig.outbox, NotifyCfg(), lambda m: True, clock=rig.clock)
    assert channel.send(n)
    rig.drain()
    (text,) = sent_texts(rig)
    assert "NA" not in text.split() and "Stop NA" not in text
    assert EVID in text
    if which == "entry":
        assert "BUY 51 shares of SBIN at the next open (last close Rs 194.40)" in text
    else:
        assert "SELL 51 shares of SBIN at the next open" in text and "BUY" not in text
        assert "stop" in text.lower()


def test_the_payload_names_no_symbol_or_side(trades):
    t = trades.trades()["SBIN"]
    payload = trades.desk.notification(t, "entry", at(L[6], "15:45")).payload
    assert not {"series_key", "trading_symbol", "side"} & set(payload)


class Refusing:
    """A notifier on which every channel refuses (all False), then accepts."""

    def __init__(self) -> None:
        self.refuse = True
        self.sent: list = []

    def send(self, n, dedup_key=None):
        self.sent.append(n)
        return {"dashboard": False, "telegram": False} if self.refuse else {"dashboard": True}


def test_an_alert_no_channel_took_is_not_marked_told_and_retried_after_five_minutes(tmp_path):
    w = World(tmp_path)
    w.notifier = Refusing()
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert all(t.entry_told_at is None for t in w.trades().values())
    tried = len(w.notifier.sent)
    w.poll(at(L[3], "15:47"))
    assert len(w.notifier.sent) == tried  # not more often than every five minutes
    w.notifier.refuse = False
    w.poll(at(L[3], "15:50"))
    assert all(t.entry_told_at is not None for t in w.trades().values())
    assert w.desk.errors >= 2
    told = len(w.notifier.sent)
    w.poll(at(L[3], "15:56"))
    assert len(w.notifier.sent) == told
    assert dt.timedelta(minutes=5)  # RETELL
