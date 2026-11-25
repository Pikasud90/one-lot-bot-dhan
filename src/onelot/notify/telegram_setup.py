"""Setting Telegram up from the Settings tab (US-087): find the chat id, send a test message.

The bot token is a secret. Everything here that can fail returns plain words built from the
HTTP status and Telegram's own description, never from an exception's text (which can quote
the request address, and so the token), and every spelling of the token is masked and
registered with the log redactor before the first request.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import TELEGRAM_TOKEN_SHAPE
from .telegram import (
    TIMEOUT,
    SendResult,
    TelegramClient,
    _harden_logging,
    _json_object,
    _plain,
    _scrub,
    api_base,
)

log = logging.getLogger(__name__)

NO_UPDATES = (
    "No chat found yet. Send any message to your bot first (open it in Telegram and press "
    "Start), then press Find my chat id again."
)
TOKEN_REFUSED = "The token was refused by Telegram. Check it, paste it again and save."
NO_TOKEN = "Save the bot token first."
#: httpx errors that mean no connection could be opened at all: what a network that blocks
#: the bot address looks like (seen on 25 Nov 2026 on an institute's network: the two
#: addresses of api.telegram.org timed out while Telegram's other sites answered)
_CONNECT_ERRORS = ("ConnectTimeout", "ConnectError")
BLOCKED_HINT = (
    " This network may be blocking api.telegram.org, the address these messages go through: "
    "office and institute networks often do. Use another network, such as a phone hotspot, "
    "or ask the network's admin to allow it."
)
_CAP_NAME = 40
MAX_CHATS = 10
TEST_TEXT = (
    "<b>One Lot Desk</b>\nTest message: Telegram alerts are set up. "
    "Trade alerts and Dhan token reminders will arrive here."
)


@dataclass(frozen=True)
class Chat:
    """What the owner needs to pick a chat: its id and a name. Nothing else is kept."""

    id: str
    name: str
    kind: str  # private | group | supergroup | channel

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "kind": self.kind}


@dataclass(frozen=True)
class ChatSearch:
    ok: bool
    message: str
    chats: list[Chat] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "chats": [c.as_dict() for c in self.chats]}


def _chat_of(update: Mapping[str, Any]) -> Chat | None:
    """The chat of one update (a message, an edited one, a channel post or a membership
    change), reduced to id, kind and a first name or title."""
    for key in ("message", "edited_message", "channel_post", "my_chat_member"):
        holder = update.get(key)
        chat = holder.get("chat") if isinstance(holder, Mapping) else None
        if not isinstance(chat, Mapping):
            continue
        chat_id = chat.get("id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            continue
        kind = _plain(chat.get("type")) or "private"
        name = _plain(chat.get("title")) or _plain(chat.get("first_name"))
        return Chat(str(chat_id), name[:_CAP_NAME] or "(no name)", kind)
    return None


def parse_chats(body: Mapping[str, Any] | None) -> list[Chat]:
    """The distinct chats of a getUpdates answer, newest first, at most MAX_CHATS."""
    result = body.get("result") if body is not None else None
    if not isinstance(result, list):
        return []
    seen: dict[str, Chat] = {}
    for update in reversed(result):
        chat = _chat_of(update) if isinstance(update, Mapping) else None
        if chat is not None and chat.id not in seen:
            seen[chat.id] = chat
    return list(seen.values())[:MAX_CHATS]


def find_chats(token: str, http: httpx.Client | None = None) -> ChatSearch:
    """Ask Telegram (getUpdates) which chats have written to the bot. Never raises; the token
    is never part of the answer."""
    if not token:
        return ChatSearch(False, NO_TOKEN)
    if not TELEGRAM_TOKEN_SHAPE.fullmatch(token):
        return ChatSearch(
            False, "The saved token is not in the shape Telegram uses. Save it again."
        )
    forms = _harden_logging(token)
    owns = http is None
    client = http or httpx.Client(timeout=TIMEOUT)
    try:
        response = client.get(f"{api_base()}/bot{token}/getUpdates", timeout=TIMEOUT)
    except httpx.HTTPError as exc:  # the class name only: the text can quote the address
        return ChatSearch(False, unreachable_words(type(exc).__name__))
    except Exception as exc:  # noqa: BLE001 - the contract: an answer, never a raise
        log.warning("telegram getUpdates failed unexpectedly: %s", type(exc).__name__)
        return ChatSearch(False, "Could not ask Telegram. Try again.")
    finally:
        if owns:
            client.close()
    body = _json_object(response)
    status = response.status_code
    if status in (401, 404):
        return ChatSearch(False, TOKEN_REFUSED)
    if status == 409:
        return ChatSearch(
            False,
            "Telegram says this bot has a webhook set, so it cannot list chats. Remove the "
            "webhook, or type the chat id by hand.",
        )
    if not 200 <= status < 300 or body is None or body.get("ok") is not True:
        raw = body.get("description") if body is not None else None
        note = _scrub(_plain(raw), forms)[:120] if isinstance(raw, str) else ""
        return ChatSearch(False, f"Telegram answered HTTP {status}{': ' + note if note else ''}.")
    chats = parse_chats(body)
    if not chats:
        return ChatSearch(False, NO_UPDATES)
    return ChatSearch(True, f"{len(chats)} chat{'s' if len(chats) != 1 else ''} found.", chats)


def unreachable_words(error: str) -> str:
    """Plain words for a request that never reached Telegram, from the error's class name
    only. A connection that could not be opened at all gets the hint that the network may
    be blocking the bot address; anything else is worth trying again."""
    if error in _CONNECT_ERRORS:
        return f"Could not reach Telegram ({error}).{BLOCKED_HINT}"
    return f"Could not reach Telegram ({error}). Try again."


def result_words(result: SendResult) -> str:
    """The plain reason a test message did not go; 'sent' for one that did."""
    if result.ok:
        return "sent"
    detail = result.detail
    if result.kind == "unavailable":
        return f"not sent: {detail}"
    if result.kind == "transient" and detail.removeprefix("network error: ") in _CONNECT_ERRORS:
        error = detail.removeprefix("network error: ")
        return f"not sent: could not reach Telegram ({error}).{BLOCKED_HINT}"
    if result.detail.startswith(("HTTP 401", "HTTP 404")):
        return "not sent: Telegram refused the token. Paste it again and save."
    if result.detail.startswith("HTTP 403"):
        return "not sent: the bot cannot write to this chat. Open your bot and press Start."
    if result.detail.startswith("HTTP 400") and "chat not found" in detail.lower():
        return "not sent: Telegram cannot find that chat. Use Find my chat id."
    return f"not sent: {detail}"


def send_test(client: TelegramClient) -> tuple[bool, str]:
    """Send one test message right now (not through the outbox: the owner is waiting for the
    answer) and say 'sent' or why not."""
    result = client.send_message(TEST_TEXT)
    return result.ok, result_words(result)
