"""US-087 AC2, AC3, AC4 (routes), AC5: Telegram on the phone, end to end against a fake.

A REAL Engine (never started: no feed, no threads but the Telegram outbox worker) over a
throw-away project folder, the real FastAPI app over it, and a fake Telegram on 127.0.0.1
(TELEGRAM_API_BASE). Nothing here reaches api.telegram.org or any other host.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from notify.payloads import exit_event, index_alert
from onelot.api.server import create_app
from onelot.clock import now_ist
from onelot.config import AppConfig, Secrets
from onelot.engine import Engine
from onelot.notify.base import Notification, NotificationLevel
from onelot.store.schema import telegram_outbox

from .fake_telegram import FAKE_CHAT, FAKE_TOKEN, FakeTelegram

with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
API_TOKEN = "test-session-token"
HEAD = {"x-onelot-token": API_TOKEN}
L = NotificationLevel


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTelegram]:
    server = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_API_BASE", server.url)
    # set-then-write: monkeypatch puts these back (or removes them) after the test
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DB_URL"):
        monkeypatch.setenv(name, "")
    yield server
    server.close()


@pytest.fixture
def desk_root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


def build_engine(root: Path) -> Engine:
    cfg = AppConfig.load(root / "config" / "config.yaml", project_root=root)
    cfg = cfg.model_copy(update={"notify": cfg.notify.model_copy(update={"os_toast": False})})
    return Engine(cfg, Secrets(api_token="x" * 40), use_mock=True)


@pytest.fixture
def engine(fake: FakeTelegram, desk_root: Path) -> Iterator[Engine]:
    eng = build_engine(desk_root)
    yield eng
    eng._stop_telegram()
    eng.db.dispose()


@pytest.fixture
def client(engine: Engine) -> TestClient:
    return TestClient(create_app(engine, API_TOKEN), raise_server_exceptions=False)


def wait_for(check: Callable[[], bool], what: str, seconds: float = 25.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def save_keys(client: TestClient, token: str = FAKE_TOKEN, chat: str = FAKE_CHAT) -> dict:
    r = client.post(
        "/api/settings/telegram", json={"bot_token": token, "chat_id": chat}, headers=HEAD
    )
    assert r.status_code == 200, r.text
    return r.json()


def settle(engine: Engine, fake: FakeTelegram, expect: int) -> None:
    """Wait for `expect` messages and an empty queue, then a beat for any stray extra."""
    wait_for(lambda: len(fake.sent) >= expect, f"{expect} messages")
    wait_for(lambda: engine.telegram_status()["queue_length"] == 0, "an empty queue")
    time.sleep(0.3)
    assert len(fake.sent) == expect, fake.sent


def live_alert(**over: Any) -> dict[str, Any]:
    now = now_ist().replace(microsecond=0)
    alert = index_alert(
        created_at=now.isoformat(),
        expires_at=(now + dt.timedelta(minutes=10)).isoformat(),
        manage={"ratchet": {"extend": True}},
        **over,
    )
    return alert


def send(engine: Engine, n: Notification) -> dict[str, bool]:
    return engine.notifier.send(n)


# ── saving puts the channel into use, in place ──────────────────────────────────
def test_saving_the_token_and_chat_through_settings_turns_the_phone_channel_on(
    engine: Engine, client: TestClient
) -> None:
    before = client.get("/api/settings").json()["telegram"]
    assert before["token"] == "not set" and before["in_use"] is False
    assert [c["name"] for c in engine.notifier.status()["channels"]] == ["dashboard"]
    state = save_keys(client)
    assert state == {
        "token": "set",
        "chat_id_set": True,
        "in_use": True,
        "master_switch_on": True,
        "problem": None,
    }
    names = [c["name"] for c in engine.notifier.status()["channels"]]
    assert names == ["dashboard", "telegram"]
    save_keys(client)  # saved again: still one phone channel
    assert [c["name"] for c in engine.notifier.status()["channels"]].count("telegram") == 1
    env = (engine.cfg.project_root / ".env").read_text()
    assert f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}" in env and f"TELEGRAM_CHAT_ID={FAKE_CHAT}" in env
    assert ((engine.cfg.project_root / ".env").stat().st_mode & 0o777) == 0o600


def test_the_token_alone_is_saved_and_the_page_is_told_what_is_missing(
    client: TestClient,
) -> None:
    r = client.post("/api/settings/telegram", json={"bot_token": FAKE_TOKEN}, headers=HEAD)
    state = r.json()
    assert state["token"] == "set" and state["chat_id_set"] is False and state["in_use"] is False
    assert "chat id" in state["problem"].lower()


def test_a_save_needs_the_session_token(client: TestClient) -> None:
    r = client.post("/api/settings/telegram", json={"bot_token": FAKE_TOKEN})
    assert r.status_code == 401
    for path in ("/chats", "/test"):
        assert client.post(f"/api/settings/telegram{path}").status_code == 401


# ── the test message ────────────────────────────────────────────────────────────
def test_the_test_message_arrives_and_the_route_says_sent(
    client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)
    r = client.post("/api/settings/telegram/test", headers=HEAD)
    assert r.json() == {"ok": True, "message": "sent"}
    assert len(fake.sent) == 1 and "Test message" in fake.sent[0]
    request = [req for req in fake.requests if req[1].endswith("/sendMessage")][0]
    assert request[2]["chat_id"] == FAKE_CHAT


def test_the_test_message_says_why_it_failed_in_plain_words(
    client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)
    fake.send_status = 403
    out = client.post("/api/settings/telegram/test", headers=HEAD).json()
    assert out["ok"] is False and "press Start" in out["message"]


def test_the_test_message_without_keys_says_what_is_missing(client: TestClient) -> None:
    out = client.post("/api/settings/telegram/test", headers=HEAD).json()
    assert out["ok"] is False and "TELEGRAM_BOT_TOKEN is not set" in out["message"]


# ── what reaches the phone (one test per row of the table) ───────────────────────
def test_row_signal_a_desk_alert_arrives_with_its_fields(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)
    alert = live_alert()
    send(
        engine,
        Notification(
            title="BUY BANKNIFTY", body="b", level=L.SIGNAL, signal_id=alert["id"], payload=alert
        ),
    )
    settle(engine, fake, 1)
    text = fake.sent[0]
    for part in (
        "BUY BANKNIFTY",  # instrument and side
        "25 NOV 45000 CE",  # the contract
        "Entry 312.40 ask",
        "Stop 250.10",
        "Target 380.00",
        "Managed: stop above breakeven",  # the line on how the trade is managed
        "Valid till",
    ):
        assert part in text, (part, text)


def test_row_stop_the_move_your_stop_note_arrives_once_per_move(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)
    note = lambda stop: Notification(  # noqa: E731
        title=f"NIFTY BANK BUY CE: move your stop to {stop}",
        body="Levels on the underlying",
        level=L.WARNING,
        position_id="p1",
        kind="stop",
        dedup_key=f"desk:ratchet:p1:{stop}",
    )
    send(engine, note("45,010.00"))
    send(engine, note("45,010.00"))  # the same move raised again
    send(engine, note("45,060.00"))  # a later move
    settle(engine, fake, 2)
    assert "move your stop to 45,010.00" in fake.sent[0]
    assert "move your stop to 45,060.00" in fake.sent[1]


def test_row_exit_the_exit_signal_arrives_once_and_is_not_repeated(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)
    event = exit_event("STOP")
    for _ in range(3):  # the desk tells it once; even if it were raised again: no second message
        send(
            engine,
            Notification(
                title="EXIT", body="b", level=L.EXIT, position_id="alert-x", payload=event
            ),
        )
    settle(engine, fake, 1)
    assert fake.sent[0].startswith("<b>EXIT: SELL")


def test_row_critical_a_critical_desk_state_arrives(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)
    engine.notifier.critical("Index feed disagreement", "the second source differs by 0.9%")
    settle(engine, fake, 1)
    assert "CRITICAL" in fake.sent[0] and "Index feed disagreement" in fake.sent[0]


def test_row_token_a_token_warning_arrives_and_a_repeat_does_not(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)

    def token(key: str, title: str) -> Notification:
        return Notification(
            title=title, body="Renew it.", level=L.WARNING, kind="token", dedup_key=key
        )

    send(engine, token("token:E:30", "Dhan token: 30 minutes left"))
    send(engine, token("token:E:30", "Dhan token: 30 minutes left"))
    send(engine, token("token:E:15", "Dhan token: 15 minutes left"))
    settle(engine, fake, 2)
    assert "30 minutes left" in fake.sent[0] and "15 minutes left" in fake.sent[1]


def test_row_volume_a_volume_warning_arrives_and_a_repeat_does_not(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)

    def volume(key: str, title: str) -> Notification:
        return Notification(
            title=title, body="3.2x its usual", level=L.WARNING, kind="volume", dedup_key=key
        )

    send(engine, volume("volume:RELIANCE:1030", "Volume: RELIANCE"))
    send(engine, volume("volume:RELIANCE:1030", "Volume: RELIANCE"))
    send(engine, volume("volume:RELIANCE:1045", "Volume: RELIANCE again"))
    settle(engine, fake, 2)
    assert "Volume: RELIANCE" in fake.sent[0] and "again" in fake.sent[1]


def test_an_ordinary_warning_and_an_info_stay_off_the_phone(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    save_keys(client)
    engine.notifier.warning("Recovered open position(s)", "check them")
    engine.notifier.info("Live market data ON")
    send(engine, Notification(title="t", body="b", level=L.WARNING, kind="stop"))  # control
    settle(engine, fake, 1)
    assert "Recovered" not in " ".join(fake.sent) and "Live market" not in " ".join(fake.sent)


def test_the_token_reminder_step_reaches_the_phone_through_the_engine(
    engine: Engine, client: TestClient, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_keys(client)
    now = now_ist().replace(microsecond=0)
    monkeypatch.setattr(engine, "token_expiry", lambda: now + dt.timedelta(minutes=23))
    engine.secrets.client_id, engine.secrets.access_token = "1100", "tok-not-real"  # has creds
    engine._token_reminders._read_expiry = engine.token_expiry  # type: ignore[attr-defined]
    engine._remind_about_the_token(now)
    settle(engine, fake, 1)
    assert "Dhan token: 23 minutes left" in fake.sent[0]
    assert "Settings, Dhan card: Renew, or paste a new token." in fake.sent[0]
    engine._remind_about_the_token(now + dt.timedelta(minutes=1))
    time.sleep(0.5)
    assert len(fake.sent) == 1  # nothing more until the next stage


# ── AC3: the token never leaves ─────────────────────────────────────────────────
GET_SKIP = {"/api/stream", "/api/session"}  # /api/session hands out the SESSION token, not this one


def every_get(client: TestClient, engine: Engine) -> str:
    seen = []
    for route in create_app(engine, API_TOKEN).routes:
        path = getattr(route, "path", "")
        if "GET" in getattr(route, "methods", ()) and path.startswith("/api") and "{" not in path:
            if path in GET_SKIP:
                continue
            r = client.get(path)
            seen.append(f"{path} {r.status_code} {r.text}")
    return "\n".join(seen)


def test_after_saving_no_get_answer_and_no_log_line_holds_the_token(
    engine: Engine, client: TestClient, fake: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    # the app's own redactor, as setup_logging attaches it to every handler
    from onelot.logging_setup import get_redactor

    caplog.handler.addFilter(get_redactor())
    secret = FAKE_TOKEN.split(":")[1]
    posts = [
        client.post(
            "/api/settings/telegram",
            json={"bot_token": FAKE_TOKEN, "chat_id": FAKE_CHAT},
            headers=HEAD,
        ),
        client.post("/api/settings/telegram/test", headers=HEAD),
        client.post("/api/settings/telegram/chats", headers=HEAD),
    ]
    send(engine, Notification(title="t", body="b", level=L.CRITICAL))
    settle(engine, fake, 2)
    engine.notifier.critical("again", "x")
    answers = every_get(client, engine) + "".join(p.text for p in posts)
    assert FAKE_TOKEN not in answers and secret not in answers
    state = client.get("/api/settings").json()["telegram"]
    assert state["token"] == "set"
    status = client.get("/api/notify/status").text
    assert FAKE_TOKEN not in status and secret not in status
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_TOKEN not in logs and secret not in logs
    # the proof it is the redactor at work: httpx did log the request addresses
    httpx_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert httpx_lines and all("REDACTED" in line for line in httpx_lines if "/bot" in line)
    # the access log of the fake saw the real thing: the proof the secret was in play
    assert any(FAKE_TOKEN in p for p in fake.paths())


def test_a_failing_send_does_not_put_the_token_in_the_status_or_the_logs(
    engine: Engine, client: TestClient, fake: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    from onelot.logging_setup import get_redactor

    caplog.set_level(logging.DEBUG)
    caplog.handler.addFilter(get_redactor())
    save_keys(client)
    fake.send_status = 500
    engine.notifier.critical("Feed dead", "no bars")
    wait_for(lambda: engine.telegram_status()["last_error_class"] is not None, "a failure")
    status = client.get("/api/notify/status").json()
    assert status["telegram"]["last_error_class"] == "HTTP 500"
    assert status["telegram"]["queue_length"] == 1
    assert FAKE_TOKEN not in json.dumps(status)
    assert FAKE_TOKEN not in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-token",
        "123:short",
        "12345:" + "a" * 29,
        "1234567890123:" + "a" * 30,
        "424242424:FAKEFAKEFAKEfakefakefake_not_real!!",
        "424242424 :" + "a" * 35,
    ],
)
def test_a_wrong_shaped_token_is_refused_with_a_sentence_and_is_not_written(
    engine: Engine, client: TestClient, bad: str
) -> None:
    r = client.post("/api/settings/telegram", json={"bot_token": bad}, headers=HEAD)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "does not look like a bot token" in detail and "Nothing was saved" in detail
    assert bad not in r.text  # the refusal never echoes what was typed
    assert not (engine.cfg.project_root / ".env").exists()
    assert client.get("/api/settings").json()["telegram"]["token"] == "not set"


def test_a_wrong_chat_id_is_refused_and_not_written(engine: Engine, client: TestClient) -> None:
    for bad in ("abc", "0123", "12.5", "@name"):
        r = client.post("/api/settings/telegram", json={"chat_id": bad}, headers=HEAD)
        assert r.status_code == 400 and "Nothing was saved" in r.json()["detail"]
    assert not (engine.cfg.project_root / ".env").exists()


def test_nothing_to_save_is_a_plain_400(client: TestClient) -> None:
    r = client.post("/api/settings/telegram", json={}, headers=HEAD)
    assert r.status_code == 400 and r.json()["detail"] == "nothing to save"


# ── AC4 through the route ───────────────────────────────────────────────────────
def private_update(chat: int = 777) -> dict:
    return {
        "update_id": 1,
        "message": {"chat": {"id": chat, "first_name": "Asha", "type": "private"}},
    }


def test_find_my_chat_id_lists_the_chat_and_choosing_it_saves_it_and_the_test_arrives(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    client.post("/api/settings/telegram", json={"bot_token": FAKE_TOKEN}, headers=HEAD)
    fake.updates = [
        private_update(),
        {"update_id": 2, "message": {"chat": {"id": -100123, "title": "Fam", "type": "group"}}},
    ]
    found = client.post("/api/settings/telegram/chats", headers=HEAD).json()
    assert found["ok"] and {c["id"] for c in found["chats"]} == {"777", "-100123"}
    assert all(set(c) == {"id", "name", "kind"} for c in found["chats"])
    state = client.post("/api/settings/telegram", json={"chat_id": "777"}, headers=HEAD).json()
    assert state["in_use"] is True  # the chat id alone turned the channel on
    assert client.post("/api/settings/telegram/test", headers=HEAD).json()["ok"] is True


def test_find_my_chat_id_before_any_message_says_to_write_to_the_bot_first(
    client: TestClient, fake: FakeTelegram
) -> None:
    client.post("/api/settings/telegram", json={"bot_token": FAKE_TOKEN}, headers=HEAD)
    found = client.post("/api/settings/telegram/chats", headers=HEAD).json()
    assert found["ok"] is False and "send any message to your bot first" in found["message"].lower()


def test_find_my_chat_id_with_a_refused_token_says_so(
    client: TestClient, fake: FakeTelegram
) -> None:
    other = "535353535:" + "OTHERotherOTHERotherOTHERother_fake_1"
    client.post("/api/settings/telegram", json={"bot_token": other}, headers=HEAD)
    found = client.post("/api/settings/telegram/chats", headers=HEAD).json()
    assert found == {
        "ok": False,
        "message": "The token was refused by Telegram. Check it, paste it again and save.",
        "chats": [],
    }


def test_find_my_chat_id_without_a_saved_token_says_to_save_it(client: TestClient) -> None:
    found = client.post("/api/settings/telegram/chats", headers=HEAD).json()
    assert found["ok"] is False and "Save the bot token first" in found["message"]


# ── AC5: a desk without Telegram keys ───────────────────────────────────────────
def test_a_desk_without_telegram_keys_changes_nothing(
    engine: Engine, client: TestClient, fake: FakeTelegram
) -> None:
    alert = live_alert()
    results = send(
        engine,
        Notification(title="BUY", body="b", level=L.SIGNAL, signal_id=alert["id"], payload=alert),
    )
    engine.notifier.critical("Feed dead", "x")
    assert results == {"dashboard": True}
    assert [n["level"] for n in engine.notifier.dashboard.recent()] == ["CRITICAL", "SIGNAL"]
    with engine.db.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(telegram_outbox)).scalar() == 0
    status = client.get("/api/notify/status").json()
    assert status["telegram"] == {
        "configured": False,
        "in_use": False,
        "last_sent_at": None,
        "last_error_class": None,
        "queue_length": 0,
    }
    assert fake.requests == []  # not one request to anywhere
    assert engine.telegram_client is None


def test_a_desk_with_a_start_up_that_finds_keys_in_the_environment_builds_the_channel(
    desk_root: Path, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT)
    eng = build_engine(desk_root)
    try:
        eng._start_telegram()
        assert eng.telegram_state()["in_use"] is True
        assert [c["name"] for c in eng.notifier.status()["channels"]] == ["dashboard", "telegram"]
    finally:
        eng._stop_telegram()
        eng.db.dispose()


def test_clearing_the_master_switch_in_the_config_is_explained(
    engine: Engine, client: TestClient
) -> None:
    engine.cfg = engine.cfg.model_copy(
        update={"notify": engine.cfg.notify.model_copy(update={"telegram": False})}
    )
    state = save_keys(client)
    assert state["in_use"] is False and "switched off" in state["problem"]
