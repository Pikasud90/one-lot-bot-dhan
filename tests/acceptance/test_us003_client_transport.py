"""US-003 AC3 (and the thread-safety / no-retry NFRs): TelegramClient.send_message.

Every test talks to the client through ``httpx.MockTransport``; the autouse guard turns any
real connection attempt into a failure (AC6). The injected httpx.Client deliberately has a 3 s
default timeout, so only a per-request timeout chosen by the client itself can produce the 10 s
the story demands.
"""

from __future__ import annotations

import dataclasses
import inspect
import threading
import time
import typing
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from us003_helpers import (
    CHAT,
    DOT,
    EMOJI_CHART,
    RUPEE,
    TOKEN,
    Recorder,
    install_network_guard,
    json_response,
    make_client,
    make_http,
    ok_response,
    payload_of,
    telegram,
    tg_error,
    truthy_flag,
)

TEN_SECONDS = {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}
FAIL_KINDS = ("rate_limited", "permanent", "transient", "unavailable")


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the client must only use the injected transport: {attempts}"


def send(client: Any, text: str = "hello", **kw: Any) -> Any:
    return client.send_message(text, **kw)


# ========================= the public contract =========================================
def test_send_result_is_a_frozen_dataclass_with_exactly_the_contract_fields():
    SendResult = telegram().SendResult
    assert dataclasses.is_dataclass(SendResult)
    assert [f.name for f in dataclasses.fields(SendResult)] == [
        "ok",
        "kind",
        "message_id",
        "retry_after",
        "detail",
    ]
    r = SendResult(True, "ok", 1, None, "")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_send_result_kind_is_a_literal_of_exactly_the_five_kinds():
    hints = typing.get_type_hints(telegram().SendResult)
    assert set(typing.get_args(hints["kind"])) == {
        "ok",
        "rate_limited",
        "permanent",
        "transient",
        "unavailable",
    }


def test_the_public_callables_are_fully_type_annotated():
    tg = telegram()
    callables = [
        tg.format_alert,
        tg.format_exit,
        tg.format_missed,
        tg.TelegramClient.__init__,
        tg.TelegramClient.send_message,
        tg.TelegramClient.from_env,
    ]
    for fn in callables:
        sig = inspect.signature(fn)
        for name, param in sig.parameters.items():
            if name not in ("self", "cls"):
                assert param.annotation is not inspect.Parameter.empty, f"{fn.__qualname__}.{name}"
        assert sig.return_annotation is not inspect.Signature.empty, fn.__qualname__


def test_a_valid_client_is_available_with_no_reason():
    client, _ = make_client()
    assert client.available is True
    assert client.unavailable_reason is None


# ========================= the request =================================================
def test_send_message_posts_send_message_to_the_bot_api_with_html_parse_mode():
    client, rec = make_client()
    send(client, "hello")
    (request,) = rec.requests
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "api.telegram.org"
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = payload_of(request)
    assert body["parse_mode"] == "HTML"
    assert str(body["chat_id"]) == CHAT
    assert body["text"] == "hello"


def test_the_token_travels_only_in_the_url_path_never_in_the_body_or_headers():
    client, rec = make_client()
    send(client, "hello")
    (request,) = rec.requests
    assert TOKEN.encode() not in request.content
    assert TOKEN.split(":", 1)[1].encode() not in request.content
    assert all(TOKEN not in v for v in request.headers.values())
    assert TOKEN not in str(request.url.params)


def test_text_is_sent_verbatim_including_newlines_emoji_and_html():
    text = f"<b>BUY ITC</b>\nEntry 4,100.50 mkt {DOT} {EMOJI_CHART} {RUPEE} P&amp;L"
    client, rec = make_client()
    send(client, text)
    assert payload_of(rec.requests[0])["text"] == text


def test_disable_notification_is_sent_when_silent_is_true():
    client, rec = make_client()
    send(client, silent=True)
    assert truthy_flag(payload_of(rec.requests[0]).get("disable_notification"))


def test_disable_notification_is_absent_when_silent_is_false_or_defaulted():
    client, rec = make_client()
    send(client, silent=False)
    send(client)
    for request in rec.requests:
        assert "disable_notification" not in payload_of(request)
    assert rec.calls == 2


def test_silent_is_keyword_only():
    client, _ = make_client()
    with pytest.raises(TypeError):
        client.send_message("hello", True)


def test_every_request_has_a_ten_second_timeout_on_all_four_httpx_phases():
    client, rec = make_client()
    send(client, silent=True)
    send(client, silent=False)
    assert [r.extensions["timeout"] for r in rec.requests] == [TEN_SECONDS, TEN_SECONDS]


@pytest.mark.parametrize("default_timeout", [None, 0.5, 120.0])
def test_the_ten_second_timeout_overrides_whatever_the_injected_client_defaults_to(
    default_timeout,
):
    rec = Recorder()
    http = httpx.Client(transport=httpx.MockTransport(rec), timeout=default_timeout)
    telegram().TelegramClient(TOKEN, CHAT, http=http).send_message("x")
    assert rec.requests[0].extensions["timeout"] == TEN_SECONDS


def test_a_client_built_without_http_still_uses_ten_seconds_on_all_four_phases(monkeypatch):
    """No http injected: the client builds its own httpx.Client. The real transport is replaced
    here (the guard would stop a real connection anyway) to observe the request it builds."""
    seen: list[httpx.Request] = []

    def fake_handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_response(7)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    result = telegram().TelegramClient(TOKEN, CHAT).send_message("hello")
    assert result.ok and result.message_id == 7
    assert seen[0].extensions["timeout"] == TEN_SECONDS
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"


# ========================= ok ==========================================================
def test_http_200_with_ok_true_and_a_message_id_is_a_successful_send():
    client, _ = make_client(lambda r: ok_response(4711))
    r = send(client)
    assert (r.ok, r.kind, r.message_id, r.retry_after) == (True, "ok", 4711, None)
    assert isinstance(r.message_id, int)
    assert isinstance(r.detail, str)


def test_a_very_large_message_id_survives():
    client, _ = make_client(lambda r: ok_response(2**40 + 5))
    assert send(client).message_id == 2**40 + 5


# ========================= rate limited ================================================
@pytest.mark.parametrize(
    ("raw", "expected"), [(5, 5.0), (1.5, 1.5), (0, 0.0), (3600, 3600.0), (17, 17.0)]
)
def test_http_429_returns_rate_limited_with_retry_after_from_the_parameters(raw, expected):
    client, rec = make_client(
        lambda r: tg_error(429, "Too Many Requests: retry after 5", parameters={"retry_after": raw})
    )
    r = send(client)
    assert (r.ok, r.kind, r.message_id) == (False, "rate_limited", None)
    assert r.retry_after == expected
    assert rec.calls == 1


def test_http_429_without_retry_after_leaves_it_none():
    client, _ = make_client(lambda r: tg_error(429, "Too Many Requests"))
    r = send(client)
    assert (r.ok, r.kind, r.retry_after, r.message_id) == (False, "rate_limited", None, None)


@pytest.mark.parametrize(
    "parameters", [None, [], "soon", {}, {"retry_after": None}, {"other": 1}], ids=repr
)
def test_http_429_with_unusable_parameters_still_returns_rate_limited_and_never_raises(parameters):
    client, _ = make_client(lambda r: tg_error(429, "Too Many Requests", parameters=parameters))
    r = send(client)
    assert (r.ok, r.kind) == (False, "rate_limited")
    assert r.retry_after is None


def test_http_429_with_a_non_json_body_is_still_rate_limited():
    client, _ = make_client(lambda r: httpx.Response(429, text="<html>slow down</html>"))
    r = send(client)
    assert (r.ok, r.kind, r.retry_after) == (False, "rate_limited", None)


def test_a_rate_limit_is_reported_not_waited_out(monkeypatch):
    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the client must never sleep: the outbox owns the back-off")

    monkeypatch.setattr(time, "sleep", boom)
    client, rec = make_client(
        lambda r: tg_error(429, "Too Many Requests", parameters={"retry_after": 3600})
    )
    t0 = time.monotonic()
    r = send(client)
    assert r.retry_after == 3600.0
    assert time.monotonic() - t0 < 2
    assert rec.calls == 1


# ========================= permanent ===================================================
PERMANENT = [
    (400, "Bad Request: chat not found"),
    (401, "Unauthorized"),
    (403, "Forbidden: bot was blocked by the user"),
    (404, "Not Found"),
]


@pytest.mark.parametrize(("status", "description"), PERMANENT)
def test_http_400_401_403_404_are_permanent_failures(status, description):
    client, rec = make_client(lambda r: tg_error(status, description))
    r = send(client)
    assert (r.ok, r.kind, r.message_id, r.retry_after) == (False, "permanent", None, None)
    assert rec.calls == 1


@pytest.mark.parametrize(("status", "description"), PERMANENT)
def test_permanent_detail_is_built_from_the_status_and_the_telegram_description(
    status, description
):
    client, _ = make_client(lambda r: tg_error(status, description))
    r = send(client)
    assert str(status) in r.detail
    assert description in r.detail


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_statuses_stay_permanent_when_the_body_is_not_json(status):
    client, _ = make_client(lambda r: httpx.Response(status, text="<html>nope</html>"))
    r = send(client)
    assert (r.ok, r.kind) == (False, "permanent")
    assert str(status) in r.detail


# ========================= transient ===================================================
@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_http_5xx_is_transient(status):
    client, rec = make_client(lambda r: tg_error(status, "Internal Server Error"))
    r = send(client)
    assert (r.ok, r.kind, r.message_id, r.retry_after) == (False, "transient", None, None)
    assert str(status) in r.detail
    assert rec.calls == 1


@pytest.mark.parametrize("status", [502, 503])
def test_http_5xx_with_a_non_json_body_is_transient(status):
    client, _ = make_client(lambda r: httpx.Response(status, text="<html>bad gateway</html>"))
    r = send(client)
    assert (r.ok, r.kind) == (False, "transient")


def _raise(factory: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        raise factory(request)

    return handler


NETWORK_ERRORS = {
    "connect": lambda req: httpx.ConnectError("connection refused", request=req),
    "connect_timeout": lambda req: httpx.ConnectTimeout("connect timed out", request=req),
    "read_timeout": lambda req: httpx.ReadTimeout("read timed out", request=req),
    "write_timeout": lambda req: httpx.WriteTimeout("write timed out", request=req),
    "pool_timeout": lambda req: httpx.PoolTimeout("pool exhausted", request=req),
    "read_error": lambda req: httpx.ReadError("connection reset", request=req),
    "write_error": lambda req: httpx.WriteError("broken pipe", request=req),
    "remote_protocol": lambda req: httpx.RemoteProtocolError("server disconnected", request=req),
    "proxy": lambda req: httpx.ProxyError("proxy refused", request=req),
    "unsupported_protocol": lambda req: httpx.UnsupportedProtocol("bad scheme", request=req),
    "decoding": lambda req: httpx.DecodingError("bad gzip", request=req),
    "too_many_redirects": lambda req: httpx.TooManyRedirects("loop", request=req),
}


@pytest.mark.parametrize("name", sorted(NETWORK_ERRORS))
def test_a_network_error_is_transient_and_never_raises(name):
    client, rec = make_client(_raise(NETWORK_ERRORS[name]))
    r = send(client)
    assert (r.ok, r.kind, r.message_id, r.retry_after) == (False, "transient", None, None)
    assert r.detail
    assert rec.calls == 1


@pytest.mark.parametrize(
    "body",
    [
        {"ok": False, "error_code": 500, "description": "Bad Request: chat not found"},
        {"ok": False},
        {"ok": False, "description": None},
        {},
    ],
    ids=["ok-false-with-description", "ok-false-bare", "ok-false-null-description", "empty-object"],
)
def test_http_200_with_ok_false_is_transient(body):
    client, rec = make_client(lambda r: json_response(200, body))
    r = send(client)
    assert (r.ok, r.kind, r.message_id) == (False, "transient", None)
    assert rec.calls == 1


def test_http_200_ok_false_detail_carries_the_telegram_description():
    client, _ = make_client(
        lambda r: json_response(200, {"ok": False, "description": "Flood wait"})
    )
    assert "Flood wait" in send(client).detail


NO_MESSAGE_ID = [
    {"ok": True},
    {"ok": True, "result": {}},
    {"ok": True, "result": None},
    {"ok": True, "result": []},
    {"ok": True, "result": {"text": "no id here"}},
    {"ok": True, "result": {"message_id": None}},
]


@pytest.mark.parametrize("body", NO_MESSAGE_ID, ids=[repr(b)[:48] for b in NO_MESSAGE_ID])
def test_http_200_ok_true_without_a_message_id_is_transient(body):
    client, _ = make_client(lambda r: json_response(200, body))
    r = send(client)
    assert (r.ok, r.kind, r.message_id) == (False, "transient", None)


NOT_JSON_BODIES = [
    b"<html><body>502 Bad Gateway</body></html>",
    b"",
    b"OK",
    b"\xff\xfe\x00\x01",
    b'{"ok": true, "result": {"message_id": 7,',
    b"{'ok': True}",
]


@pytest.mark.parametrize("body", NOT_JSON_BODIES, ids=[repr(b)[:40] for b in NOT_JSON_BODIES])
def test_http_200_with_a_body_that_is_not_json_is_transient(body):
    client, _ = make_client(lambda r: httpx.Response(200, content=body))
    r = send(client)
    assert (r.ok, r.kind, r.message_id) == (False, "transient", None)


@pytest.mark.parametrize("body", [b"null", b"[]", b"42", b'"ok"', b"[1, 2]", b"true"])
def test_http_200_with_json_that_is_not_an_object_is_transient(body):
    client, _ = make_client(lambda r: httpx.Response(200, content=body))
    r = send(client)
    assert (r.ok, r.kind, r.message_id) == (False, "transient", None)


ODD_BODIES = [
    b"null",
    b"[]",
    b"42",
    b'"text"',
    b"",
    b"<html></html>",
    b'{"ok": false, "description": {"nested": 1}}',
    b'{"ok": false, "description": ["a", "b"]}',
    b'{"ok": false, "description": 12}',
    b'{"ok": false, "parameters": "str"}',
]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (200, "transient"),
        (400, "permanent"),
        (401, "permanent"),
        (429, "rate_limited"),
        (500, "transient"),
    ],
)
@pytest.mark.parametrize("body", ODD_BODIES, ids=[repr(b)[:34] for b in ODD_BODIES])
def test_odd_bodies_never_raise_and_the_kind_follows_the_status(status, kind, body):
    client, _ = make_client(lambda r: httpx.Response(status, content=body))
    r = send(client)
    assert r.kind == kind
    assert r.ok is False
    assert r.message_id is None
    assert isinstance(r.detail, str)


# ========================= unavailable =================================================
@pytest.mark.parametrize(
    ("token", "chat"),
    [("not-a-token", CHAT), (TOKEN, "@my_channel"), ("", CHAT), (TOKEN, ""), (TOKEN, "0")],
    ids=["bad-token", "channel-name", "empty-token", "empty-chat", "zero-chat"],
)
@pytest.mark.parametrize("silent", [False, True])
def test_an_unavailable_client_returns_unavailable_and_makes_no_http_call(token, chat, silent):
    client, rec = make_client(token=token, chat_id=chat)
    assert client.available is False
    r = send(client, silent=silent)
    assert (r.ok, r.kind, r.message_id, r.retry_after) == (False, "unavailable", None, None)
    assert r.detail
    assert rec.calls == 0


def test_an_unavailable_client_from_missing_env_makes_no_http_call():
    rec = Recorder()
    client = telegram().TelegramClient.from_env({}, http=make_http(rec))
    assert client.available is False
    assert send(client).kind == "unavailable"
    assert rec.calls == 0


# ========================= the client never retries ====================================
FAILURES: dict[str, Any] = {
    "http-500": lambda r: tg_error(500, "Internal Server Error"),
    "http-502-html": lambda r: httpx.Response(502, text="<html>bad gateway</html>"),
    "http-429": lambda r: tg_error(429, "Too Many Requests", parameters={"retry_after": 1}),
    "http-400": lambda r: tg_error(400, "Bad Request"),
    "http-200-ok-false": lambda r: json_response(200, {"ok": False, "description": "x"}),
    "http-200-garbage": lambda r: httpx.Response(200, content=b"garbage"),
    "http-200-no-id": lambda r: json_response(200, {"ok": True, "result": {}}),
    "connect-error": _raise(NETWORK_ERRORS["connect"]),
    "read-timeout": _raise(NETWORK_ERRORS["read_timeout"]),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_a_failed_send_makes_exactly_one_http_request(name, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: pytest.fail("client slept (a retry?)"))
    client, rec = make_client(FAILURES[name])
    r = send(client)
    assert r.ok is False
    assert rec.calls == 1


def test_consecutive_sends_are_independent_and_each_makes_one_request():
    script = [
        lambda r: tg_error(500, "boom"),
        lambda r: ok_response(11),
        lambda r: tg_error(429, "slow", parameters={"retry_after": 2}),
        lambda r: ok_response(12),
        lambda r: tg_error(403, "Forbidden"),
    ]
    state = {"i": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            i = state["i"]
            state["i"] += 1
        return script[i](request)

    client, rec = make_client(handler)
    results = [send(client, f"m{i}") for i in range(5)]
    assert [r.kind for r in results] == ["transient", "ok", "rate_limited", "ok", "permanent"]
    assert [r.message_id for r in results] == [None, 11, None, 12, None]
    assert results[2].retry_after == 2.0
    assert rec.calls == 5


# ========================= result invariants ===========================================
@pytest.mark.parametrize("name", sorted(FAILURES))
def test_failure_results_have_a_reason_and_no_stray_fields(name):
    client, _ = make_client(FAILURES[name])
    r = send(client)
    assert r.kind in FAIL_KINDS and r.kind != "ok"
    assert r.ok is False and r.message_id is None
    assert r.detail
    assert (r.retry_after is not None) is (r.kind == "rate_limited")


# ========================= thread safety ===============================================
def test_sixteen_concurrent_sends_each_get_their_own_correct_result():
    """Sixteen threads released together share one client. Each text carries its index; the
    server echoes it back as the message id (or a per-index 429 / 500), so any shared mutable
    state in the client (a reused payload, a last-result field) would cross the wires."""
    n = 16

    def respond(request: httpx.Request) -> httpx.Response:
        i = int(payload_of(request)["text"].split("-")[1])
        if i % 4 == 0:
            return tg_error(429, "slow", parameters={"retry_after": i})
        if i % 4 == 1:
            return tg_error(500, "boom")
        return ok_response(1000 + i)

    for _round in range(4):
        client, rec = make_client(respond)
        start = threading.Barrier(n, timeout=10)

        def one(i: int, client: Any = client, start: threading.Barrier = start) -> Any:
            start.wait()
            return client.send_message(f"msg-{i}", silent=(i % 2 == 0))

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(one, range(n)))

        assert rec.calls == n
        for i, r in enumerate(results):
            if i % 4 == 0:
                assert (r.kind, r.retry_after) == ("rate_limited", float(i)), i
            elif i % 4 == 1:
                assert r.kind == "transient", i
            else:
                assert (r.kind, r.message_id) == ("ok", 1000 + i), i
        seen = {}
        for request in rec.requests:
            body = payload_of(request)
            seen[body["text"]] = truthy_flag(body.get("disable_notification"))
        assert seen == {f"msg-{i}": (i % 2 == 0) for i in range(n)}


def test_sixteen_concurrent_sends_on_an_available_client_never_raise():
    client, rec = make_client(lambda r: ok_response(1))
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(client.send_message, f"t{i}") for i in range(16)]
        results = [f.result(timeout=10) for f in futures]
    assert all(r.ok for r in results)
    assert rec.calls == 16
