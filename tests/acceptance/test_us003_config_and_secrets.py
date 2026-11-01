"""US-003 AC5: configuration shape, from_env, and the Secrets integration.

The token must match ``^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$`` and the chat id
``^-?[1-9][0-9]{0,18}$``, ASCII only, whole string (a trailing newline does not sneak through
``$``). The constructor is strict; only ``from_env`` strips CR and LF. A bad or missing value
makes the client unavailable with a human-readable reason that never echoes the token.

The Secrets tests go through ``Secrets.load`` and the process environment (monkeypatched), so
they do not depend on the attribute names the implementation chooses for the new fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from us003_helpers import (
    CHAT,
    E_ACUTE,
    TOKEN,
    Recorder,
    arabic_indic,
    fullwidth,
    install_network_guard,
    leaked_forms,
    make_http,
    payload_of,
    telegram,
)

from onelot.config import Secrets
from onelot.logging_setup import get_redactor, register_secrets


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"configuration code must not touch the network: {attempts}"


def build(token: str, chat: str = CHAT, http: Any = None) -> Any:
    return telegram().TelegramClient(token, chat, http=http)


# ========================= the token shape =============================================
VALID_TOKENS = {
    "nine-digit-id (the AC4 shape)": TOKEN,
    "six-digit-id": "123456:" + "A" * 30,
    "twelve-digit-id": "123456789012:" + "A" * 30,
    "exactly-30-char-secret": "123456789:" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5",
    "long-secret": "123456789:" + "Ab_-" * 25,
    "underscore-and-dash-only": "123456789:" + "_-" * 20,
}


INVALID_TOKENS = {
    "five-digit-id": "12345:" + "A" * 35,
    "thirteen-digit-id": "1234567890123:" + "A" * 35,
    "29-char-secret": "123456789:" + "A" * 29,
    "no-colon": "123456789" + "A" * 35,
    "empty-secret": "123456789:",
    "plus-in-secret": "123456789:" + "A" * 20 + "+" + "A" * 20,
    "dot-in-secret": "123456789:" + "A" * 20 + "." + "A" * 20,
    "slash-in-secret": "123456789:" + "A" * 20 + "/" + "A" * 20,
    "space-in-secret": "123456789:" + "A" * 20 + " " + "A" * 20,
    "leading-space": " " + TOKEN,
    "trailing-space": TOKEN + " ",
    "trailing-newline": TOKEN + "\n",
    "trailing-crlf": TOKEN + "\r\n",
    "leading-newline": "\n" + TOKEN,
    "embedded-newline": "123456789:" + "A" * 20 + "\n" + "A" * 20,
    "full-width-digits-in-id": fullwidth("123456789") + ":" + "A" * 35,
    "arabic-indic-digits-in-id": arabic_indic("123456789") + ":" + "A" * 35,
    "non-ascii-letters-in-secret": "123456789:" + E_ACUTE * 35,
    "bot-prefix": "bot" + TOKEN,
    "negative-id": "-12345678:" + "A" * 35,
    "two-colons": "123456789:" + "A" * 20 + ":" + "A" * 20,
    "whole-url": f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    "empty": "",
    "whitespace-only": "   ",
}


@pytest.mark.parametrize("token", list(VALID_TOKENS.values()), ids=list(VALID_TOKENS))
def test_a_token_of_the_right_shape_is_accepted(token):
    client = build(token)
    assert client.available is True
    assert client.unavailable_reason is None


@pytest.mark.parametrize("token", list(INVALID_TOKENS.values()), ids=list(INVALID_TOKENS))
def test_a_token_of_the_wrong_shape_makes_the_client_unavailable(token):
    client = build(token)
    assert client.available is False
    assert isinstance(client.unavailable_reason, str) and client.unavailable_reason.strip()
    assert "token" in client.unavailable_reason.lower()


@pytest.mark.parametrize("token", list(INVALID_TOKENS.values()), ids=list(INVALID_TOKENS))
def test_the_unavailable_reason_never_echoes_any_part_of_the_bad_token(token):
    client = build(token)
    secret_part = token.split(":")[-1].strip()
    whole = token.strip()
    for blob in (str(client.unavailable_reason), repr(client), str(client)):
        for piece in (whole, secret_part):
            if len(piece) >= 8:
                assert piece not in blob


def test_the_valid_token_is_never_echoed_by_the_reason_when_only_the_chat_id_is_bad():
    client = build(TOKEN, "@my_channel")
    assert client.available is False
    assert not leaked_forms(str(client.unavailable_reason))


# ========================= the chat id shape ===========================================
VALID_CHATS = ["1", "-1", "123456789", "-1001234567890", "9" * 19, "-" + "9" * 19, "1" + "0" * 18]

INVALID_CHATS = {
    "empty": "",
    "zero": "0",
    "minus-zero": "-0",
    "double-zero": "00",
    "leading-zero": "01",
    "negative-leading-zero": "-01",
    "plus-sign": "+123",
    "leading-space": " 123",
    "trailing-space": "123 ",
    "trailing-newline": "123\n",
    "leading-newline": "\n123",
    "bare-minus": "-",
    "double-minus": "--1",
    "trailing-minus": "1-",
    "full-width-digits": fullwidth("1234"),
    "negative-full-width-digits": fullwidth("-1001"),
    "arabic-indic-digits": arabic_indic("123"),
    "channel-name": "@channel",
    "channel-name-2": "@my_channel_name",
    "letters": "abc",
    "decimal": "1.5",
    "exponent": "1e3",
    "hex": "0x1F",
    "inner-space": "123 456",
    "20-digits": "9" * 20,
    "negative-20-digits": "-" + "9" * 20,
}


@pytest.mark.parametrize("chat", VALID_CHATS)
def test_a_chat_id_of_the_right_shape_is_accepted(chat):
    client = build(TOKEN, chat)
    assert client.available is True
    assert client.unavailable_reason is None


@pytest.mark.parametrize("chat", list(INVALID_CHATS.values()), ids=list(INVALID_CHATS))
def test_a_chat_id_of_the_wrong_shape_makes_the_client_unavailable(chat):
    client = build(TOKEN, chat)
    assert client.available is False
    assert isinstance(client.unavailable_reason, str) and client.unavailable_reason.strip()
    assert "chat" in client.unavailable_reason.lower()


def test_both_values_bad_is_still_unavailable_with_a_reason():
    client = build("nope", "@nope")
    assert client.available is False
    assert client.unavailable_reason


# ========================= from_env ====================================================
def env(token: str | None = TOKEN, chat: str | None = CHAT) -> dict[str, str]:
    out: dict[str, str] = {}
    if token is not None:
        out["TELEGRAM_BOT_TOKEN"] = token
    if chat is not None:
        out["TELEGRAM_CHAT_ID"] = chat
    return out


def test_from_env_builds_an_available_client_from_the_two_variables():
    rec = Recorder()
    client = telegram().TelegramClient.from_env(env(), http=make_http(rec))
    assert client.available is True and client.unavailable_reason is None
    assert client.send_message("hi").ok
    (request,) = rec.requests
    assert request.url.path == f"/bot{TOKEN}/sendMessage"


@pytest.mark.parametrize(
    ("raw_token", "raw_chat"),
    [
        (TOKEN + "\n", CHAT),
        (TOKEN + "\r\n", CHAT),
        ("\n" + TOKEN, CHAT),
        ("\r\n" + TOKEN + "\r\n", CHAT),
        (TOKEN, CHAT + "\n"),
        (TOKEN, "\r" + CHAT + "\r\n"),
        (TOKEN + "\r\n", CHAT + "\r\n"),
    ],
    ids=repr,
)
def test_from_env_strips_cr_and_lf_so_the_request_uses_the_clean_values(raw_token, raw_chat):
    rec = Recorder()
    client = telegram().TelegramClient.from_env(env(raw_token, raw_chat), http=make_http(rec))
    assert client.available is True
    client.send_message("hi")
    (request,) = rec.requests
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    assert str(payload_of(request)["chat_id"]) == CHAT


@pytest.mark.parametrize(
    "raw",
    [
        TOKEN[:20] + "\n" + TOKEN[20:],
        TOKEN + "\nTELEGRAM_CHAT_ID=1",
        TOKEN[:20] + "\r\n" + TOKEN[20:],
    ],
    ids=["newline-inside", "injected-second-variable", "crlf-inside"],
)
def test_from_env_does_not_repair_a_newline_inside_the_value(raw):
    """Stripping CR and LF is for the ends only; an embedded newline would let one value write
    a second line into .env, so it must stay invalid."""
    client = telegram().TelegramClient.from_env(env(raw), http=make_http(Recorder()))
    assert client.available is False


def test_from_env_leaves_ordinary_spaces_alone_so_a_typo_is_rejected_not_silently_fixed():
    client = telegram().TelegramClient.from_env(env(" " + TOKEN), http=make_http(Recorder()))
    assert client.available is False


@pytest.mark.parametrize(
    ("environment", "word"),
    [
        ({}, "token"),
        (env(None, CHAT), "token"),
        (env("", CHAT), "token"),
        (env("\r\n", CHAT), "token"),
        (env(TOKEN, None), "chat"),
        (env(TOKEN, ""), "chat"),
        (env(TOKEN, "\n"), "chat"),
    ],
    ids=[
        "nothing",
        "no-token",
        "empty-token",
        "newline-token",
        "no-chat",
        "empty-chat",
        "newline-chat",
    ],
)
def test_from_env_with_a_missing_value_is_unavailable_and_says_which(environment, word):
    client = telegram().TelegramClient.from_env(environment, http=make_http(Recorder()))
    assert client.available is False
    assert word in str(client.unavailable_reason).lower()


def test_from_env_with_both_values_missing_is_unavailable_with_a_reason():
    client = telegram().TelegramClient.from_env({}, http=make_http(Recorder()))
    assert client.available is False
    assert client.unavailable_reason


def test_from_env_with_a_malformed_token_is_unavailable_and_does_not_echo_it():
    bad = "123456789:AAH-" + "q" * 25  # a 29-character secret: one short of the 30 minimum
    client = telegram().TelegramClient.from_env(env(bad), http=make_http(Recorder()))
    assert client.available is False
    reason = str(client.unavailable_reason)
    assert bad not in reason and "q" * 25 not in reason and "123456789" not in reason


def test_from_env_with_a_malformed_chat_id_is_unavailable():
    client = telegram().TelegramClient.from_env(env(TOKEN, "@channel"), http=make_http(Recorder()))
    assert client.available is False


def test_from_env_reads_exactly_the_two_documented_variable_names():
    wrong = {"telegram_bot_token": TOKEN, "TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT": CHAT}
    client = telegram().TelegramClient.from_env(wrong, http=make_http(Recorder()))
    assert client.available is False


def test_from_env_accepts_any_read_only_mapping_and_does_not_modify_it():
    mapping = MappingProxyType(env())
    client = telegram().TelegramClient.from_env(mapping, http=make_http(Recorder()))
    assert client.available is True
    assert dict(mapping) == env()


def test_from_env_defaults_to_the_live_process_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert telegram().TelegramClient.from_env(http=make_http(Recorder())).available is True
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert telegram().TelegramClient.from_env(http=make_http(Recorder())).available is False


def test_constructing_a_client_without_http_makes_no_network_attempt():
    client = build(TOKEN)  # the autouse guard fails the test on any socket or DNS use
    assert client.available is True


# ========================= Secrets integration =========================================
TG_TOKEN = "987654321:AAH-" + "k3Jd9Qw2LmZ7pRt5Vx8Yb1Nc4Hs6Fg0Ta"
TG_SECRET = TG_TOKEN.split(":", 1)[1]
TG_CHAT = "-1009876543210"
DHAN_VARS = (
    "DHAN_CLIENT_ID",
    "DHAN_ACCESS_TOKEN",
    "DHAN_DATA_ACCESS_TOKEN",
    "DHAN_PIN",
    "DHAN_TOTP_SECRET",
    "ONELOT_API_TOKEN",
)


def load_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token: str | None = TG_TOKEN,
    chat: str | None = TG_CHAT,
) -> Secrets:
    for name in DHAN_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return Secrets.load(project_root=tmp_path)


def test_env_keys_include_the_telegram_token_and_chat_id_and_keep_the_dhan_keys():
    assert "TELEGRAM_BOT_TOKEN" in Secrets.ENV_KEYS
    assert "TELEGRAM_CHAT_ID" in Secrets.ENV_KEYS
    for existing in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "DHAN_DATA_ACCESS_TOKEN"):
        assert existing in Secrets.ENV_KEYS
    assert len(set(Secrets.ENV_KEYS)) == len(Secrets.ENV_KEYS)


def test_secret_values_include_the_telegram_token(monkeypatch, tmp_path):
    secrets = load_secrets(monkeypatch, tmp_path)
    assert TG_TOKEN in secrets.secret_values()


def test_secret_values_do_not_include_the_chat_id(monkeypatch, tmp_path):
    """AC5 lists the chat id with the secret keys but AC4 and ADR-002 amendment 8 forbid
    registering it with the redactor (it would corrupt numbers in logs), and secret_values()
    is exactly the list that gets registered. So: ENV_KEYS yes, secret_values no."""
    secrets = load_secrets(monkeypatch, tmp_path)
    assert TG_CHAT not in secrets.secret_values()
    register_secrets(secrets.secret_values())  # what __main__ and the engine do with the list
    line = f"chat {TG_CHAT} offset 7"
    assert get_redactor().scrub(line) == line


def test_secret_values_without_telegram_configured_stay_clean(monkeypatch, tmp_path):
    secrets = load_secrets(monkeypatch, tmp_path, token=None, chat=None)
    assert all("TELEGRAM" not in v and "AAH-" not in v for v in secrets.secret_values())
    assert "" not in secrets.secret_values()


def test_masked_never_shows_any_part_of_the_telegram_token(monkeypatch, tmp_path):
    secrets = load_secrets(monkeypatch, tmp_path)
    blob = json.dumps(secrets.masked(), default=str)
    for piece in (TG_TOKEN, TG_SECRET, TG_SECRET[-4:], TG_SECRET[-8:], TG_SECRET[:8], "987654321"):
        assert piece not in blob, "masked() leaks part of the Telegram token"


def test_masked_reports_the_telegram_token_only_as_set_or_not_set(monkeypatch, tmp_path):
    when_set = load_secrets(monkeypatch, tmp_path).masked()
    when_unset = load_secrets(monkeypatch, tmp_path, token=None, chat=None).masked()
    keys_set = {k: v for k, v in when_set.items() if "telegram" in k.lower()}
    keys_unset = {k: v for k, v in when_unset.items() if "telegram" in k.lower()}
    assert keys_set, f"masked() has no telegram entry: {sorted(when_set)}"
    assert keys_set.keys() == keys_unset.keys()
    assert keys_set != keys_unset, "masked() cannot tell a configured token from a missing one"


def test_masked_is_json_serialisable_with_telegram_configured(monkeypatch, tmp_path):
    json.dumps(load_secrets(monkeypatch, tmp_path).masked())


def test_repr_and_str_of_secrets_do_not_show_the_telegram_token(monkeypatch, tmp_path):
    secrets = load_secrets(monkeypatch, tmp_path)
    for text in (repr(secrets), str(secrets)):
        assert TG_TOKEN not in text and TG_SECRET not in text


def test_secrets_still_accepts_the_existing_constructor_arguments():
    secrets = Secrets(client_id="1000000001", access_token="tok-abcdefgh1234", pin="1234")
    assert "tok-abcdefgh1234" in secrets.secret_values()
    assert secrets.has_broker_creds is True
