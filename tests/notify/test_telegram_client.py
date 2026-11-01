"""US-003 AC3: TelegramClient.send_message turns every outcome into a SendResult.

No network: every client runs over httpx.MockTransport. The token is fake.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from onelot.notify import telegram as tg

TOKEN = "123456789:AAH-" + "x" * 35  # fake, the AC4 shape
CHAT = "-1001234567890"
OK_BODY = {"ok": True, "result": {"message_id": 42, "chat": {"id": -1001234567890}, "text": "t"}}

Respond = Callable[[httpx.Request], httpx.Response]


class Wire:
    """A MockTransport handler that records each request and answers with `respond`."""

    def __init__(self, respond: Respond) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def client_for(respond: Respond, *, token: str = TOKEN, chat: str = CHAT) -> tuple[Any, Wire]:
    wire = Wire(respond)
    # The http client's own default timeout is 3 s, so only a per-request 10 s passes AC3.
    http = httpx.Client(transport=httpx.MockTransport(wire), timeout=3.0)
    return tg.TelegramClient(token, chat, http=http), wire


def answer(status: int, body: Any = None, *, content: bytes | None = None) -> Respond:
    if content is not None:
        return lambda request: httpx.Response(status, content=content)
    return lambda request: httpx.Response(status, json=body)


def raising(make: Callable[[httpx.Request], Exception]) -> Respond:
    def respond(request: httpx.Request) -> httpx.Response:
        raise make(request)

    return respond


def tg_error(status: int, description: str, **extra: Any) -> Respond:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    body.update(extra)
    return answer(status, body)


class TestRequest:
    def test_posts_send_message_as_json_with_html_parse_mode(self):
        client, wire = client_for(answer(200, OK_BODY))
        client.send_message("<b>BUY SBIN · 1 share</b>")
        assert len(wire.requests) == 1
        request = wire.requests[0]
        assert request.method == "POST"
        assert (request.url.scheme, request.url.host) == ("https", "api.telegram.org")
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        assert request.headers["content-type"].startswith("application/json")
        body = json.loads(request.content)
        assert body["chat_id"] == CHAT
        assert body["text"] == "<b>BUY SBIN · 1 share</b>"
        assert body["parse_mode"] == "HTML"
        assert "disable_notification" not in body

    def test_disable_notification_is_sent_only_when_silent(self):
        client, wire = client_for(answer(200, OK_BODY))
        client.send_message("quiet", silent=True)
        client.send_message("loud", silent=False)
        quiet, loud = (json.loads(r.content) for r in wire.requests)
        assert quiet["disable_notification"] is True
        assert "disable_notification" not in loud

    def test_every_request_has_a_10_second_timeout_on_all_four_phases(self):
        client, wire = client_for(answer(200, OK_BODY))
        client.send_message("t")
        assert wire.requests[0].extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }


class TestClassification:
    def test_ok_returns_the_message_id(self):
        client, _ = client_for(answer(200, OK_BODY))
        result = client.send_message("t")
        assert isinstance(result, tg.SendResult)
        assert (result.ok, result.kind, result.message_id, result.retry_after) == (
            True,
            "ok",
            42,
            None,
        )
        assert isinstance(result.detail, str)

    def test_429_is_rate_limited_with_retry_after(self):
        respond = tg_error(429, "Too Many Requests: retry after 7", parameters={"retry_after": 7})
        result = client_for(respond)[0].send_message("t")
        assert (result.ok, result.kind, result.message_id) == (False, "rate_limited", None)
        assert result.retry_after == 7.0

    @pytest.mark.parametrize(
        "respond",
        [tg_error(429, "Too Many Requests"), answer(429, content=b"slow down")],
        ids=["no-parameters", "not-json"],
    )
    def test_429_without_retry_after_gives_none(self, respond):
        result = client_for(respond)[0].send_message("t")
        assert (result.kind, result.retry_after) == ("rate_limited", None)

    @pytest.mark.parametrize(
        ("status", "description"),
        [
            (400, "Bad Request: chat not found"),
            (401, "Unauthorized"),
            (403, "Forbidden: bot was blocked by the user"),
            (404, "Not Found"),
        ],
    )
    def test_permanent_failures(self, status, description):
        result = client_for(tg_error(status, description))[0].send_message("t")
        assert (result.ok, result.kind, result.message_id) == (False, "permanent", None)
        assert str(status) in result.detail
        assert description in result.detail

    @pytest.mark.parametrize(
        "respond",
        [
            tg_error(500, "Internal Server Error"),
            answer(502, content=b"<html>Bad Gateway</html>"),
            tg_error(503, "Service Unavailable"),
            raising(lambda req: httpx.ConnectError("connection refused", request=req)),
            raising(lambda req: httpx.ReadTimeout("timed out", request=req)),
            answer(200, {"ok": False, "description": "odd"}),
            answer(200, content=b"<html>captive portal</html>"),
            answer(200, {"ok": True, "result": {}}),
            answer(200, {"ok": True, "result": {"message_id": None}}),
            answer(200, {"ok": True, "result": True}),
            answer(200, content=b"[1, 2]"),
        ],
        ids=[
            "500",
            "502-html",
            "503",
            "connect-error",
            "read-timeout",
            "200-ok-false",
            "200-not-json",
            "200-no-message-id",
            "200-null-message-id",
            "200-result-not-object",
            "200-json-list",
        ],
    )
    def test_transient_failures_are_returned_not_raised(self, respond):
        result = client_for(respond)[0].send_message("t")
        assert (result.ok, result.kind, result.message_id) == (False, "transient", None)

    @pytest.mark.parametrize(
        ("token", "chat"),
        [("", CHAT), (TOKEN, ""), ("not-a-token", CHAT)],
        ids=["no-token", "no-chat-id", "bad-token"],
    )
    def test_an_unavailable_client_makes_no_http_call(self, token, chat):
        client, wire = client_for(answer(200, OK_BODY), token=token, chat=chat)
        assert client.available is False
        result = client.send_message("t")
        assert (result.ok, result.kind, result.message_id) == (False, "unavailable", None)
        assert wire.requests == []

    @pytest.mark.parametrize(
        "respond",
        [
            tg_error(500, "Internal Server Error"),
            tg_error(429, "Too Many Requests", parameters={"retry_after": 1}),
            raising(lambda req: httpx.ConnectError("connection refused", request=req)),
            answer(200, {"ok": False}),
        ],
        ids=["500", "429", "connect-error", "200-ok-false"],
    )
    def test_the_client_never_retries(self, respond):
        client, wire = client_for(respond)
        client.send_message("t")
        assert len(wire.requests) == 1

    def test_send_result_is_frozen(self):
        result = client_for(answer(200, OK_BODY))[0].send_message("t")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.ok = False  # type: ignore[misc]


class TestEdgeCases:
    """Dev Beta T2: boundaries and hostile bodies around the AC3 classification."""

    @pytest.mark.parametrize(
        ("status", "kind"),
        [(409, "permanent"), (413, "permanent"), (418, "permanent"), (408, "transient")],
    )
    def test_other_client_errors_are_permanent_except_408(self, status, kind):
        """A 4xx fails the same way on every retry; 408 is a proxy that timed out."""
        result = client_for(tg_error(status, "nope"))[0].send_message("t")
        assert (result.ok, result.kind) == (False, kind)

    @pytest.mark.parametrize("status", [301, 302, 304, 307])
    def test_redirects_are_not_followed_and_are_transient(self, status):
        respond = lambda request: httpx.Response(  # noqa: E731
            status, headers={"location": "https://example.invalid/"}
        )
        client, wire = client_for(respond)
        assert client.send_message("t").kind == "transient"
        assert len(wire.requests) == 1

    def test_any_2xx_with_a_message_id_is_ok(self):
        result = client_for(answer(201, OK_BODY))[0].send_message("t")
        assert (result.ok, result.kind, result.message_id) == (True, "ok", 42)

    def test_a_boolean_message_id_is_not_a_message_id(self):
        respond = answer(200, {"ok": True, "result": {"message_id": True}})
        assert client_for(respond)[0].send_message("t").kind == "transient"

    def test_a_text_that_cannot_be_encoded_is_permanent_and_sends_nothing(self):
        client, wire = client_for(answer(200, OK_BODY))
        result = client.send_message("BUY " + chr(0xD800))  # a lone surrogate
        assert (result.ok, result.kind, result.message_id) == (False, "permanent", None)
        assert wire.requests == []

    def test_absurdly_nested_json_is_transient_not_a_crash(self):
        result = client_for(answer(200, content=b"[" * 100_000))[0].send_message("t")
        assert result.kind == "transient"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"1.5", 1.5),
            (b"0", 0.0),
            (b"-3", None),
            (b"NaN", None),
            (b"Infinity", None),
            (b"9" * 400, None),  # a JSON integer too large for a float
            (b"true", None),
            (b'"7"', None),
            (b"null", None),
        ],
        ids=repr,
    )
    def test_retry_after_is_a_finite_non_negative_number_or_none(self, raw, expected):
        body = b'{"ok": false, "error_code": 429, "parameters": {"retry_after": ' + raw + b"}}"
        result = client_for(answer(429, content=body))[0].send_message("t")
        assert (result.kind, result.retry_after) == ("rate_limited", expected)

    def test_detail_is_one_line_and_capped(self):
        description = "Bad Request:\n" + "x" * 1000
        result = client_for(tg_error(400, description))[0].send_message("t")
        assert "\n" not in result.detail
        assert result.detail.startswith("HTTP 400: Bad Request: xxx")
        assert len(result.detail) <= len("HTTP 400: ") + 200

    @pytest.mark.parametrize("description", [None, 12, ["a"], {"b": 1}, ""])
    def test_a_missing_or_odd_description_leaves_just_the_status(self, description):
        respond = answer(500, {"ok": False, "description": description})
        assert client_for(respond)[0].send_message("t").detail == "HTTP 500"

    def test_network_errors_name_only_the_error_class(self):
        respond = raising(lambda req: httpx.ConnectError(f"refused {req.url}", request=req))
        result = client_for(respond)[0].send_message("t")
        assert result.detail == "network error: ConnectError"

    def test_close_closes_only_an_http_client_it_built(self):
        injected = httpx.Client(transport=httpx.MockTransport(answer(200, OK_BODY)))
        tg.TelegramClient(TOKEN, CHAT, http=injected).close()
        assert injected.is_closed is False
        owned = tg.TelegramClient(TOKEN, CHAT)
        owned.close()
        assert owned._http is not None and owned._http.is_closed is True
        tg.TelegramClient("", "").close()  # nothing built, nothing to close

    def test_after_close_the_client_is_unavailable_and_sends_nothing(self):
        client, wire = client_for(answer(200, OK_BODY))
        client.close()
        assert client.available is False
        assert client.unavailable_reason == "the Telegram client is closed"
        result = client.send_message("t")
        assert (result.ok, result.kind, result.message_id) == (False, "unavailable", None)
        assert wire.requests == []

    def test_close_keeps_the_original_reason_of_an_unconfigured_client(self):
        client = tg.TelegramClient("", CHAT)
        reason = client.unavailable_reason
        client.close()
        assert client.unavailable_reason == reason

    @pytest.mark.parametrize("error", [RuntimeError, ValueError, KeyError, OSError])
    def test_an_unexpected_exception_is_transient_and_names_only_its_class(self, error):
        """send_message never raises; the exception text (which may quote the URL, and so
        the token) never reaches the result."""
        client, _ = client_for(raising(lambda req: error(f"boom at {req.url}")))
        result = client.send_message("t")
        assert (result.ok, result.kind) == (False, "transient")
        assert result.detail == f"unexpected error: {error.__name__}"
        assert TOKEN.split(":", 1)[1] not in repr(result)
