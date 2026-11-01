"""US-003 AC5: Telegram credentials — shape checks, from_env, and the Secrets wiring.

Non-ASCII look-alikes are built with chr() so this file holds only plain ASCII digits.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from onelot.config import Secrets
from onelot.notify import telegram as tg

TOKEN = "123456789:AAH-" + "x" * 35  # fake, the AC4 shape
OTHER_TOKEN = "987654321:" + "k" * 31 + "Q7zW"  # fake, distinctive last four characters
CHAT = "-1001234567890"
FULLWIDTH_DIGITS = "".join(chr(0xFF10 + int(d)) for d in "123456789")  # U+FF11..U+FF19
ARABIC_INDIC_123 = chr(0x0661) + chr(0x0662) + chr(0x0663)
CYRILLIC_A = chr(0x0430)

OK_BODY = {"ok": True, "result": {"message_id": 7}}


def mock_http(seen: list[httpx.Request] | None = None) -> httpx.Client:
    def respond(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    return httpx.Client(transport=httpx.MockTransport(respond))


def client(token: str = TOKEN, chat: str = CHAT) -> Any:
    return tg.TelegramClient(token, chat, http=mock_http())


class TestTokenShape:
    @pytest.mark.parametrize(
        "token",
        ["123456:" + "a" * 30, "123456789012:" + "Az09_-" * 5, TOKEN, OTHER_TOKEN],
        ids=["shortest", "longest-id", "ac4-shape", "other"],
    )
    def test_valid_tokens(self, token):
        c = client(token=token)
        assert c.available is True
        assert c.unavailable_reason is None

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "12345:" + "a" * 30,
            "1234567890123:" + "a" * 30,
            "123456789:" + "a" * 29,
            "123456789:" + "a" * 34 + "!",
            " " + TOKEN,
            TOKEN + " ",
            TOKEN + "\n",
            "+" + TOKEN,
            "bot" + TOKEN,
            FULLWIDTH_DIGITS + ":" + "a" * 35,
            "123456789:" + CYRILLIC_A * 35,
            "123456789" + "a" * 35,
            "123456789::" + "a" * 35,
        ],
        ids=[
            "empty",
            "5-digit-id",
            "13-digit-id",
            "29-char-secret",
            "bad-char",
            "leading-space",
            "trailing-space",
            "trailing-newline",
            "plus-sign",
            "bot-prefix",
            "fullwidth-digits",
            "cyrillic-secret",
            "no-colon",
            "double-colon",
        ],
    )
    def test_invalid_tokens_make_the_client_unavailable_without_echoing_them(self, token):
        c = client(token=token)
        assert c.available is False
        reason = c.unavailable_reason
        assert isinstance(reason, str) and "token" in reason.lower()
        if token.strip():
            assert token.strip() not in reason
            secret = token.split(":", 1)[-1].strip()
            assert len(secret) < 6 or secret not in reason


class TestChatIdShape:
    @pytest.mark.parametrize("chat", ["1", "123456789", CHAT, "9" * 19, "-" + "9" * 19], ids=str)
    def test_valid_chat_ids(self, chat):
        assert client(chat=chat).available is True

    @pytest.mark.parametrize(
        "chat",
        [
            "",
            "0",
            "-0",
            "0123",
            "+123",
            " 123",
            "123 ",
            "123\n",
            FULLWIDTH_DIGITS[:3],
            ARABIC_INDIC_123,
            "@channel",
            "12a",
            "1" * 20,
            "--1",
            "-",
            "1_000",
        ],
        ids=[
            "empty",
            "zero",
            "minus-zero",
            "leading-zero",
            "plus-sign",
            "leading-space",
            "trailing-space",
            "trailing-newline",
            "fullwidth-digits",
            "arabic-indic-digits",
            "at-channel",
            "letter",
            "20-digits",
            "double-minus",
            "bare-minus",
            "underscore",
        ],
    )
    def test_invalid_chat_ids_make_the_client_unavailable(self, chat):
        c = client(chat=chat)
        assert c.available is False
        assert isinstance(c.unavailable_reason, str) and "chat" in c.unavailable_reason.lower()
        assert TOKEN not in c.unavailable_reason


class TestFromEnv:
    def test_strips_cr_and_lf(self):
        seen: list[httpx.Request] = []
        env = {"TELEGRAM_BOT_TOKEN": TOKEN + "\r\n", "TELEGRAM_CHAT_ID": "\r\n" + CHAT + "\n"}
        c = tg.TelegramClient.from_env(env, http=mock_http(seen))
        assert c.available is True
        assert c.send_message("t").ok is True
        assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"

    def test_strips_only_cr_and_lf_so_a_leading_space_is_still_rejected(self):
        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": " 123"}
        assert tg.TelegramClient.from_env(env, http=mock_http()).available is False

    @pytest.mark.parametrize(
        ("env", "names"),
        [
            ({}, "TELEGRAM_BOT_TOKEN"),
            ({"TELEGRAM_CHAT_ID": CHAT}, "TELEGRAM_BOT_TOKEN"),
            ({"TELEGRAM_BOT_TOKEN": TOKEN}, "TELEGRAM_CHAT_ID"),
            ({"TELEGRAM_BOT_TOKEN": "123:short", "TELEGRAM_CHAT_ID": CHAT}, "TELEGRAM_BOT_TOKEN"),
            ({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "@me"}, "TELEGRAM_CHAT_ID"),
        ],
        ids=["nothing", "no-token", "no-chat-id", "bad-token", "bad-chat-id"],
    )
    def test_missing_or_invalid_values_name_the_variable(self, env, names):
        c = tg.TelegramClient.from_env(env, http=mock_http())
        assert c.available is False
        assert names in c.unavailable_reason

    def test_the_reason_never_echoes_an_invalid_token(self):
        bad = "123456789:" + "s3cr3t" * 6 + "!"
        c = tg.TelegramClient.from_env(
            {"TELEGRAM_BOT_TOKEN": bad, "TELEGRAM_CHAT_ID": CHAT}, http=mock_http()
        )
        assert c.available is False
        assert "s3cr3t" not in c.unavailable_reason
        assert "123456789" not in c.unavailable_reason

    def test_reads_os_environ_by_default(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
        seen: list[httpx.Request] = []
        c = tg.TelegramClient.from_env(http=mock_http(seen))
        assert c.available is True
        c.send_message("t")
        assert len(seen) == 1


class TestSecretsWiring:
    def test_env_keys_include_both_telegram_variables(self):
        assert "TELEGRAM_BOT_TOKEN" in Secrets.ENV_KEYS
        assert "TELEGRAM_CHAT_ID" in Secrets.ENV_KEYS

    def test_secrets_carry_the_telegram_values(self):
        s = Secrets(telegram_bot_token=TOKEN, telegram_chat_id=CHAT)
        assert (s.telegram_bot_token, s.telegram_chat_id) == (TOKEN, CHAT)

    def test_secret_values_hold_three_token_forms_and_never_the_chat_id(self):
        """The redactor gets the token, the part after the colon and the %3A form (AC4);
        the chat id is not a secret and would corrupt numbers in logs (ADR-002 #8)."""
        values = Secrets(telegram_bot_token=TOKEN, telegram_chat_id=CHAT).secret_values()
        assert TOKEN in values
        assert TOKEN.split(":", 1)[1] in values
        assert TOKEN.replace(":", "%3A") in values
        assert not any(CHAT.lstrip("-") in v for v in values)

    def test_masked_reports_the_token_only_as_set(self):
        masked = Secrets(telegram_bot_token=OTHER_TOKEN, telegram_chat_id=CHAT).masked()
        assert masked["telegram_bot_token"] == "set"
        assert "Q7zW" not in repr(masked)
        assert OTHER_TOKEN.split(":", 1)[1] not in repr(masked)
        assert Secrets().masked()["telegram_bot_token"] is None

    def test_repr_never_shows_the_token(self):
        s = Secrets(telegram_bot_token=TOKEN, telegram_chat_id=CHAT)
        assert TOKEN.split(":", 1)[1] not in repr(s) and TOKEN.split(":", 1)[1] not in str(s)

    def test_load_reads_the_telegram_variables(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
        monkeypatch.setenv("ONELOT_API_TOKEN", "a" * 40)
        s = Secrets.load(tmp_path)
        assert (s.telegram_bot_token, s.telegram_chat_id) == (TOKEN, CHAT)
