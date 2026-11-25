"""US-087: what the phone channel forwards (token, volume, stop), the loopback-only test hook
for the API address, and "Find my chat id" on recorded getUpdates shapes. No network.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from onelot.config import NotifyCfg
from onelot.notify.base import Notification, NotificationLevel
from onelot.notify.telegram import API_BASE, api_base
from onelot.notify.telegram_channel import TelegramChannel
from onelot.notify.telegram_setup import (
    NO_UPDATES,
    TOKEN_REFUSED,
    find_chats,
    result_words,
)

from .test_telegram_outbox import T0, TOKEN, Rig, error, ok

L = NotificationLevel


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def build(respond: Callable[[httpx.Request], httpx.Response] = lambda r: ok()) -> Rig:
        rig = Rig(tmp_path / f"u{len(made)}.db", respond)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        rig.outbox.stop()
        rig.db.dispose()


def channel_of(rig: Rig, in_session: bool = True) -> TelegramChannel:
    return TelegramChannel(
        rig.client, rig.outbox, NotifyCfg(), lambda m: in_session, clock=rig.clock
    )


def warning(kind: str, key: str | None, title: str = "T", body: str = "B", **payload: object):
    return Notification(
        title=title, body=body, level=L.WARNING, kind=kind, dedup_key=key, payload=dict(payload)
    )


# ── what is forwarded ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["token", "volume", "stop"])
def test_a_warning_of_kind_token_volume_or_stop_is_queued_and_sent(make_rig, kind) -> None:
    rig = make_rig()
    ch = channel_of(rig)
    assert ch.accepts(warning(kind, f"{kind}:1"))
    assert ch.send(warning(kind, f"{kind}:1", title=f"{kind} title", body="the body")) is True
    assert rig.drain() == 1
    text = rig.requests[0].content.decode()
    assert f"{kind} title" in text and "the body" in text
    assert rig.rows()[0]["kind"] == kind


@pytest.mark.parametrize("kind", ["warning", "generic", "info", ""])
def test_any_other_warning_stays_off_the_phone(make_rig, kind) -> None:
    rig = make_rig()
    ch = channel_of(rig)
    n = warning(kind, "x:1")
    assert not ch.accepts(n)
    assert ch.send(n) is False
    assert rig.rows() == []


def test_info_is_still_not_for_the_phone(make_rig) -> None:
    rig = make_rig()
    assert not channel_of(rig).accepts(
        Notification(title="t", body="b", level=L.INFO, kind="token")
    )


def test_a_repeated_key_is_held_back_and_a_new_key_is_never_held_back(make_rig) -> None:
    rig = make_rig()
    ch = channel_of(rig)
    ch.send(warning("volume", "volume:RELIANCE:1000"))
    ch.send(warning("volume", "volume:RELIANCE:1000"))  # the same alert raised again
    ch.send(warning("volume", "volume:RELIANCE:1010"))  # a new one
    ch.send(warning("token", "token:E:60"))
    ch.send(warning("token", "token:E:60"))
    assert [r["dedup_key"] for r in rig.rows()] == [
        "volume:RELIANCE:1000",
        "volume:RELIANCE:1010",
        "token:E:60",
    ]
    assert rig.drain() == 3


def test_a_token_reminder_always_has_a_sound_and_the_volume_note_is_quiet_outside_the_session(
    make_rig,
) -> None:
    rig = make_rig()
    ch = channel_of(rig, in_session=False)
    ch.send(warning("token", "token:E:5"))
    ch.send(warning("volume", "volume:A:1"))
    by_kind = {r["kind"]: r["silent"] for r in rig.rows()}
    assert by_kind == {"token": False, "volume": True}


def test_a_stage_reminder_expires_with_the_token_and_the_expired_one_never_does(make_rig) -> None:
    rig = make_rig()
    ch = channel_of(rig)
    deadline = (T0 + dt.timedelta(minutes=5)).isoformat()
    ch.send(warning("token", "token:E:5", valid_until=deadline))
    ch.send(warning("token", "token:E:expired"))
    rig.clock.now = T0 + dt.timedelta(minutes=6)  # the phone was offline until after the expiry
    rig.drain()
    status = {r["dedup_key"]: r["status"] for r in rig.rows()}
    assert status == {"token:E:5": "skipped", "token:E:expired": "sent"}


def test_the_queue_depth_shows_in_the_channel_status(make_rig) -> None:
    rig = make_rig()
    ch = channel_of(rig)
    ch.send(warning("token", "token:E:60"))
    assert ch.status()["queue_depth"] == 1


# ── TELEGRAM_API_BASE: loopback only ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:8790", "http://localhost:8790/", "https://localhost", "http://127.0.0.1"],
)
def test_a_loopback_base_is_accepted(value: str) -> None:
    assert api_base({"TELEGRAM_API_BASE": value}) == value.rstrip("/")


@pytest.mark.parametrize(
    "value",
    [
        "https://api.telegram.org.evil.example",
        "http://example.com:8790",
        "http://127.0.0.1.evil.example",
        "http://[::1]:8790",
        "http://user@evil.example/",
        "ftp://127.0.0.1",
        "127.0.0.1:8790",
        "http://127.0.0.1:99999",
        "http://",
    ],
)
def test_any_other_base_is_ignored_with_one_warning(value: str, caplog) -> None:
    caplog.set_level(logging.WARNING)
    assert api_base({"TELEGRAM_API_BASE": value}) == API_BASE
    assert api_base({"TELEGRAM_API_BASE": value}) == API_BASE
    warns = [r for r in caplog.records if "TELEGRAM_API_BASE" in r.getMessage()]
    assert len(warns) == 1


def test_no_base_means_the_real_address() -> None:
    assert api_base({}) == API_BASE == "https://api.telegram.org"


# ── Find my chat id ──────────────────────────────────────────────────────────────
def chats_over(response: httpx.Response) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: response))


def updates(*items: dict) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": list(items)})


def test_a_private_chat_is_listed_with_its_first_name_and_id_only() -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "from": {"id": 777, "first_name": "Asha", "last_name": "Rao", "username": "asha_r"},
            "chat": {
                "id": 777,
                "first_name": "Asha",
                "last_name": "Rao",
                "username": "asha_r",
                "type": "private",
            },
            "date": 1,
            "text": "/start",
        },
    }
    found = find_chats(TOKEN, chats_over(updates(update)))
    assert found.ok
    assert found.as_dict()["chats"] == [{"id": "777", "name": "Asha", "kind": "private"}]
    blob = str(found.as_dict())
    assert "Rao" not in blob and "asha_r" not in blob and "/start" not in blob


def test_a_group_is_listed_by_its_title_with_a_negative_id() -> None:
    update = {
        "update_id": 2,
        "message": {
            "message_id": 6,
            "from": {"id": 777, "first_name": "Asha"},
            "chat": {"id": -1001234567890, "title": "Family desk", "type": "supergroup"},
            "text": "hi",
        },
    }
    found = find_chats(TOKEN, chats_over(updates(update)))
    assert found.chats[0].as_dict() == {
        "id": "-1001234567890",
        "name": "Family desk",
        "kind": "supergroup",
    }


def test_the_same_chat_twice_is_listed_once_and_a_membership_update_counts() -> None:
    private = {"message": {"chat": {"id": 7, "first_name": "A", "type": "private"}}}
    joined = {"my_chat_member": {"chat": {"id": -5, "title": "G", "type": "group"}}}
    found = find_chats(TOKEN, chats_over(updates(private, private, joined)))
    assert [c.id for c in found.chats] == ["-5", "7"]


def test_no_updates_yet_says_to_send_a_message_first() -> None:
    found = find_chats(TOKEN, chats_over(updates()))
    assert not found.ok and found.message == NO_UPDATES
    assert "send any message to your bot first" in found.message.lower()


def test_updates_without_a_chat_count_as_no_updates() -> None:
    found = find_chats(TOKEN, chats_over(updates({"update_id": 9, "poll": {}}, {"junk": 1})))
    assert found.message == NO_UPDATES


@pytest.mark.parametrize("status", [401, 404])
def test_a_refused_token_says_so(status: int) -> None:
    found = find_chats(TOKEN, chats_over(error(status, "Unauthorized")))
    assert not found.ok and found.message == TOKEN_REFUSED
    assert "refused" in found.message


def test_a_webhook_conflict_is_explained() -> None:
    found = find_chats(TOKEN, chats_over(error(409, "Conflict: can't use getUpdates")))
    assert "webhook" in found.message


def test_a_network_failure_names_only_the_error_class_never_the_address() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    found = find_chats(TOKEN, httpx.Client(transport=httpx.MockTransport(boom)))
    assert not found.ok and "ConnectError" in found.message
    assert TOKEN not in found.message and "bot" not in found.message.replace("your bot", "")


def test_an_error_description_that_echoes_the_token_is_masked() -> None:
    found = find_chats(TOKEN, chats_over(error(502, f"Bad gateway for /bot{TOKEN}/getUpdates")))
    assert TOKEN not in found.message and TOKEN.split(":")[1] not in found.message


def test_no_token_and_a_wrong_shaped_token_make_no_request() -> None:
    calls: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return updates()

    client = httpx.Client(transport=httpx.MockTransport(spy))
    assert not find_chats("", client).ok
    assert not find_chats("not-a-token", client).ok
    assert calls == []


# ── the test message's reasons ───────────────────────────────────────────────────
def test_the_test_message_reasons_are_plain() -> None:
    from onelot.notify.telegram import SendResult

    assert result_words(SendResult(True, "ok", 1, None, "HTTP 200")) == "sent"
    refused = SendResult(False, "permanent", None, None, "HTTP 401: Unauthorized")
    assert "refused the token" in result_words(refused)
    blocked = SendResult(False, "permanent", None, None, "HTTP 403: Forbidden: bot was blocked")
    assert "press Start" in result_words(blocked)
    nochat = SendResult(False, "permanent", None, None, "HTTP 400: Bad Request: chat not found")
    assert "Find my chat id" in result_words(nochat)


# ── a network that blocks the bot address (found on 25 Nov 2026 on an institute's network) ──
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout, httpx.ConnectError])
def test_a_connection_that_cannot_be_opened_says_the_network_may_block_telegram(exc) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc(f"cannot connect to {request.url}")

    found = find_chats(TOKEN, httpx.Client(transport=httpx.MockTransport(boom)))
    assert not found.ok and exc.__name__ in found.message
    assert "This network may be blocking api.telegram.org" in found.message
    assert "phone hotspot" in found.message and "Try again" not in found.message
    assert TOKEN not in found.message and TOKEN.split(":")[1] not in found.message


def test_an_answer_that_times_out_part_way_still_says_try_again() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the answer did not come")

    found = find_chats(TOKEN, httpx.Client(transport=httpx.MockTransport(slow)))
    assert found.message == "Could not reach Telegram (ReadTimeout). Try again."


def test_the_test_message_says_so_too_when_the_network_blocks_telegram() -> None:
    from onelot.notify.telegram import SendResult

    for name in ("ConnectTimeout", "ConnectError"):
        words = result_words(SendResult(False, "transient", None, None, f"network error: {name}"))
        assert words.startswith(f"not sent: could not reach Telegram ({name}).")
        assert "This network may be blocking api.telegram.org" in words
    other = SendResult(False, "transient", None, None, "network error: ReadTimeout")
    assert result_words(other) == "not sent: network error: ReadTimeout"
