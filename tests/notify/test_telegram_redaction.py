"""US-003 AC4 and AC6 (unit level): the bot token never leaks; the suite never reaches out.

Telegram puts the secret in the URL path (/bot<token>/sendMessage), and httpx logs every
request URL at INFO, so the token is looked for everywhere it could surface: SendResult,
repr/str of the client, and every log record captured on a handler WITHOUT the global
redaction filter (setup_logging puts its filter on its own handlers only).

Each test builds its own fake token (`fake_token`): the redactor is process-wide, so a token
registered by one test must never make another one pass.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from onelot.logging_setup import get_redactor
from onelot.notify import telegram as tg

CHAT = "-1009876543210"
# Captured when this module is imported (at collection, before any fixture runs), so a test
# can tell whether the autouse socket guard has replaced them without touching the network.
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_CONNECT = socket.socket.connect

Respond = Callable[[httpx.Request], httpx.Response]


def fake_token(tag: str) -> str:
    """A valid-shaped fake token unique to `tag` (35 characters after the colon)."""
    return "555000111:T" + hashlib.sha256(tag.encode()).hexdigest()[:34]


def spellings(token: str) -> dict[str, str]:
    secret = token.split(":", 1)[1]
    return {
        "full token": token,
        "bot+token": f"bot{token}",
        "part after the colon": secret,
        "%3A form": token.replace(":", "%3A"),
    }


def leaked(token: str, blob: str) -> list[str]:
    """Which spellings occur in `blob` (names only, never the values)."""
    return [name for name, form in spellings(token).items() if form in blob]


class _Bare(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def bare_capture() -> Iterator[_Bare]:
    """A filterless root handler with root and httpx opened to DEBUG. Enter it AFTER the
    client is built, so a level the client sets cannot hide what this looks for."""
    handler = _Bare()
    root, httpx_log = logging.getLogger(), logging.getLogger("httpx")
    saved = (root.level, httpx_log.level)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    httpx_log.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(saved[0])
        httpx_log.setLevel(saved[1])


def rendered(records: list[logging.LogRecord]) -> str:
    """Everything a handler, formatter or log shipper could see in the records."""
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    parts: list[str] = []
    for record in records:
        parts += [record.getMessage(), repr(record.msg), repr(record.args)]
        parts += [record.exc_text or "", record.stack_info or "", fmt.format(record)]
    return "\n".join(parts)


@pytest.fixture
def restore_logger_levels() -> Iterator[None]:
    names = ("httpx", "httpcore", "httpcore.connection", "httpcore.http11")
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


# ── the five AC4 failure modes, plus the plain success that logs the URL too ─────────────
def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"All connection attempts failed for {request.url}", request=request)


class _RaisesForStatus(httpx.Client):
    """What a caller of raise_for_status() sees: an HTTPStatusError quoting the URL."""

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        response = httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        raise httpx.HTTPStatusError(
            f"Client error '401 Unauthorized' for url '{request.url}'",
            request=request,
            response=response,
        )


def _non_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=f"<html>bad gateway for {request.url}</html>")


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout(f"timed out reading {request.url}", request=request)


def _bad_json(request: httpx.Request) -> httpx.Response:
    body = f'{{"ok": true, "echo": "{request.url}", "result": {{"message_id": '
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})


def mock_http(respond: Respond = _ok) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(respond))


MODES: dict[str, Respond | None] = {
    "connect_error": _connect_error,
    "status_error": None,  # the http client itself raises
    "non_json": _non_json,
    "timeout": _timeout,
    "bad_json": _bad_json,
    "ok": _ok,
}
ANSWERED = ("non_json", "bad_json", "ok")  # httpx logs a request line only for a response


def client_in(mode: str, token: str) -> tg.TelegramClient:
    respond = MODES[mode]
    if respond is None:
        http: httpx.Client = _RaisesForStatus(transport=httpx.MockTransport(_ok))
    else:
        http = mock_http(respond)
    return tg.TelegramClient(token, CHAT, http=http)


class TestNoLeakInAnyFailureMode:
    @pytest.mark.parametrize("mode", sorted(MODES))
    def test_no_spelling_of_the_token_reaches_any_surface(self, mode):
        token = fake_token(f"surface-{mode}")
        client = client_in(mode, token)
        with bare_capture() as logs:
            result = client.send_message("hello")
        if mode in ANSWERED:  # control: the request line WAS logged, so a leak is detectable
            assert any(r.name == "httpx" for r in logs.records), "httpx logged nothing"
        surfaces = {
            "SendResult fields": " ".join(
                str(getattr(result, f.name)) for f in dataclasses.fields(result)
            ),
            "SendResult repr": repr(result),
            "client repr": repr(client),
            "client str": str(client),
            "log records": rendered(logs.records),
        }
        for where, blob in surfaces.items():
            assert not leaked(token, blob), f"{where} leaks {leaked(token, blob)} ({mode})"

    @pytest.mark.parametrize("mode", sorted(MODES))
    def test_send_message_never_raises_and_detail_never_quotes_the_exception(self, mode):
        result = client_in(mode, fake_token(f"detail-{mode}")).send_message("hello")
        for exception_text in ("All connection", "Client error", "timed out", "bad gateway"):
            assert exception_text not in result.detail
        assert result.detail

    @pytest.mark.parametrize("status", [400, 401, 404, 500])
    def test_a_description_that_echoes_the_token_is_scrubbed_from_detail(self, status):
        token = fake_token(f"echo-{status}")
        for form in spellings(token).values():
            description = f"Not Found: /{form}/sendMessage"
            respond = lambda request, d=description: httpx.Response(  # noqa: E731
                status, json={"ok": False, "error_code": status, "description": d}
            )
            client = tg.TelegramClient(token, CHAT, http=mock_http(respond))
            result = client.send_message("hello")
            assert not leaked(token, result.detail + repr(result)), (status, form[:3])
            assert result.detail.startswith(f"HTTP {status}: Not Found: /")


class TestRedactorWiring:
    def test_building_a_client_registers_the_three_token_forms(self):
        token = fake_token("registered")
        tg.TelegramClient(token, CHAT, http=mock_http())
        literals = set(get_redactor()._literals)
        assert {token, token.replace(":", "%3A"), token.split(":", 1)[1]} <= literals

    def test_a_malformed_but_token_like_value_is_registered_too(self):
        """A token one character short is still mostly the secret (a typo in .env)."""
        token = fake_token("typo")[:-6]  # 29 characters after the colon: invalid shape
        client = tg.TelegramClient(token, CHAT)
        assert client.available is False
        assert token.split(":", 1)[1] in get_redactor()._literals

    def test_the_chat_id_is_never_registered_and_numbers_survive(self):
        chat = "-1004242424242"
        tg.TelegramClient(fake_token("chat"), chat, http=mock_http())
        redactor = get_redactor()
        assert chat not in redactor._literals and chat.lstrip("-") not in redactor._literals
        line = f"update 100 chat {chat} message_id 4711"
        assert redactor.scrub(line) == line

    def test_the_redactor_sits_on_the_httpx_logger_exactly_once(self):
        for n in range(3):
            tg.TelegramClient(fake_token(f"filter-{n}"), CHAT, http=mock_http())
        assert logging.getLogger("httpx").filters.count(get_redactor()) == 1

    def test_httpcore_is_raised_to_warning(self, restore_logger_levels):
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        tg.TelegramClient(fake_token("httpcore"), CHAT, http=mock_http())
        for name in ("httpcore", "httpcore.connection", "httpcore.http11"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name

    def test_a_stricter_httpcore_level_is_never_lowered(self, restore_logger_levels):
        logging.getLogger("httpcore").setLevel(logging.ERROR)
        tg.TelegramClient(fake_token("httpcore-strict"), CHAT, http=mock_http())
        assert logging.getLogger("httpcore").level == logging.ERROR

    def test_an_httpcore_child_set_lower_is_raised_and_a_stricter_one_kept(
        self, restore_logger_levels
    ):
        logging.getLogger("httpcore.http11").setLevel(logging.DEBUG)
        logging.getLogger("httpcore.connection").setLevel(logging.ERROR)
        tg.TelegramClient(fake_token("httpcore-child"), CHAT, http=mock_http())
        assert logging.getLogger("httpcore.http11").level == logging.WARNING
        assert logging.getLogger("httpcore.connection").level == logging.ERROR

    def test_a_value_without_a_colon_is_not_token_like_and_never_registered(self):
        """A chat id pasted into the token field must not scrub numbers from every log."""
        swapped = "-1007777777777"
        client = tg.TelegramClient(swapped, CHAT, http=mock_http())
        assert client.available is False
        literals = get_redactor()._literals
        assert swapped not in literals and swapped.lstrip("-") not in literals

    @settings(max_examples=60, deadline=None, derandomize=True)
    @given(
        pre=st.text(max_size=30),
        post=st.text(max_size=30),
        which=st.sampled_from(sorted(spellings(fake_token("x")))),
        style=st.sampled_from(["message", "args", "url-arg", "exception"]),
        level=st.sampled_from([logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR]),
    )
    def test_any_record_on_the_httpx_logger_reaches_a_bare_handler_scrubbed(
        self, pre, post, which, style, level
    ):
        token = fake_token("property")
        tg.TelegramClient(token, CHAT, http=mock_http())
        text = f"{pre}{spellings(token)[which]}{post}"
        log = logging.getLogger("httpx")
        with bare_capture() as logs:
            if style == "message":
                log.log(level, text)
            elif style == "args":
                log.log(level, "request failed: %s", text)
            elif style == "url-arg":
                log.log(
                    level, "HTTP Request: %s", httpx.URL(f"https://api.telegram.org/bot{token}/x")
                )
            else:
                try:
                    raise RuntimeError(text)
                except RuntimeError:
                    log.log(level, "unexpected", exc_info=True)
        assert not leaked(token, rendered(logs.records))


class TestMaskedClient:
    def test_repr_names_the_class_and_availability_and_masks_the_token(self):
        token = fake_token("repr")
        client = tg.TelegramClient(token, CHAT, http=mock_http())
        text = repr(client)
        assert text.startswith("TelegramClient(") and text.endswith(")")
        assert "available=True" in text and "token=***" in text
        assert str(client) == text
        assert not leaked(token, text + repr([client]) + f"{client!r}")

    def test_repr_of_an_unavailable_client_says_so(self):
        assert "available=False" in repr(tg.TelegramClient("", CHAT))

    def test_no_public_attribute_holds_the_token(self):
        token = fake_token("public")
        client = tg.TelegramClient(token, CHAT, http=mock_http())
        for name in dir(client):
            if name.startswith("_"):
                continue
            value = getattr(client, name)
            if not callable(value):
                assert not leaked(token, repr(value) + str(value)), name


# ── AC6: no real network, and one client shared by 16 threads ───────────────────────────
class TestHermetic:
    def test_the_socket_guard_is_autouse_in_this_package(self):
        """No fixture requested here: the guard must already be in place."""
        assert socket.getaddrinfo is not _REAL_GETADDRINFO
        assert socket.create_connection is not _REAL_CREATE_CONNECTION
        assert socket.socket.connect is not _REAL_CONNECT

    def test_the_guard_blocks_and_records_dns_and_tcp(self, network_attempts):
        assert socket.getaddrinfo is not _REAL_GETADDRINFO  # never resolve for real
        with pytest.raises(Exception):  # noqa: B017 - the guard chooses its own error
            socket.getaddrinfo("api.telegram.org", 443)
        assert network_attempts
        network_attempts.clear()

    def test_a_client_without_injected_http_is_stopped_by_the_guard(self, network_attempts):
        assert socket.create_connection is not _REAL_CREATE_CONNECTION
        client = tg.TelegramClient(fake_token("no-http"), CHAT)
        with contextlib.suppress(Exception):  # a loud guard may raise through httpx
            client.send_message("hello")
        client.close()
        assert network_attempts, "the default transport reached for the network unrecorded"
        network_attempts.clear()

    def test_local_socketpairs_still_work(self):
        a, b = socket.socketpair()
        with a, b:
            a.sendall(b"ok")
            assert b.recv(2) == b"ok"

    def test_sixteen_concurrent_sends_on_one_client_each_get_their_own_result(self):
        """16 threads released together; the server answers per message, so any per-call
        state kept on the client (a reused payload, a last result) would cross the wires."""
        n = 16
        lock = threading.Lock()
        seen: dict[str, bool] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            i = int(body["text"].split("-")[1])
            with lock:
                seen[body["text"]] = body.get("disable_notification") is True
            if i % 4 == 0:
                return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": i}})
            if i % 4 == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1000 + i}})

        client = tg.TelegramClient(fake_token("concurrent"), CHAT, http=mock_http(respond))
        start = threading.Barrier(n, timeout=10)

        def one(i: int) -> tg.SendResult:
            start.wait()
            return client.send_message(f"msg-{i}", silent=i % 2 == 0)

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(one, range(n)))
        for i, result in enumerate(results):
            if i % 4 == 0:
                assert (result.kind, result.retry_after) == ("rate_limited", float(i)), i
            elif i % 4 == 1:
                assert result.kind == "transient", i
            else:
                assert (result.kind, result.message_id) == ("ok", 1000 + i), i
        assert seen == {f"msg-{i}": i % 2 == 0 for i in range(n)}
