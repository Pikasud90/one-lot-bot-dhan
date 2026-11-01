"""US-003 AC4: the bot token can never leak.

The failure that matters: Telegram puts the secret IN THE URL (``/bot<token>/sendMessage``),
and httpx logs every request URL, httpx exceptions quote URLs, and JSON/HTTP errors echo
bodies. So these tests look for the token in every place it could surface:

* every SendResult field and its repr,
* repr/str of the client and its public attributes,
* any exception that escapes,
* every LOG RECORD captured on a bare handler that has NO redaction filter (the way a test, a
  notebook or a second handler sees it). setup_logging's filter sits on handlers, so only a
  redactor attached to the ``httpx`` logger itself protects those; ``httpcore`` must stay at
  WARNING or above so its DEBUG chatter never exists.

Four spellings are searched for: the full token, ``bot`` + token, the part after the colon, and
the percent-encoded (%3A) form. The chat id must NOT be registered with the redactor (it would
corrupt ordinary numbers in logs).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from us003_helpers import (
    CHAT,
    PCT,
    SECRET,
    TOKEN,
    TOKEN_FORMS,
    Recorder,
    capture_logs,
    install_network_guard,
    leaked_forms,
    make_client,
    make_http,
    ok_response,
    record_blob,
    telegram,
    tg_error,
)

from onelot.logging_setup import get_redactor

ROOT = Path(__file__).resolve().parents[2]
MARKER = "SENTINEL-EXC-TEXT-9d2f"
BODY_MARKER = "SENTINEL-BODY-TEXT-41ac"


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the client must only use the injected transport: {attempts}"


# ========================= the five failure modes of AC4 ===============================
def _connect_error(request: httpx.Request) -> httpx.Response:
    try:
        raise OSError(f"[Errno 61] Connection refused: {request.url}")
    except OSError as cause:
        raise httpx.ConnectError(
            f"All connection attempts failed for url '{request.url}'", request=request
        ) from cause


class _StatusErrorClient(httpx.Client):
    """An http client whose `send` does what a naive `raise_for_status()` caller would see:
    an HTTPStatusError whose message quotes the full URL."""

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        response = httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        raise httpx.HTTPStatusError(
            f"Client error '401 Unauthorized' for url '{request.url}'",
            request=request,
            response=response,
        )


def _non_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=f"<html>502 Bad Gateway while fetching {request.url}</html>")


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout(f"timed out reading from {request.url}", request=request)


def _bad_json(request: httpx.Request) -> httpx.Response:
    body = f'{{"ok": true, "echo": "{request.url}", "result": {{"message_id": '
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


def _http_for(mode: str) -> httpx.Client:
    if mode == "status_error":
        return _StatusErrorClient(transport=httpx.MockTransport(lambda r: ok_response()))
    handler = {
        "connect_error": _connect_error,
        "non_json": _non_json,
        "timeout": _timeout,
        "bad_json": _bad_json,
    }[mode]
    return make_http(handler)


MODES = ["connect_error", "status_error", "non_json", "timeout", "bad_json"]


def _run(mode: str) -> tuple[Any, Any, list[logging.LogRecord]]:
    client = telegram().TelegramClient(TOKEN, CHAT, http=_http_for(mode))  # built BEFORE capture
    with capture_logs() as logs:
        try:
            result = client.send_message("hello")
        except Exception as exc:  # the client must turn failures into a SendResult
            pytest.fail(
                f"send_message raised {type(exc).__name__} in mode {mode}; "
                f"token spellings in its text: {leaked_forms(str(exc))}"
            )
    return client, result, logs.records


@pytest.mark.parametrize("mode", MODES)
def test_no_token_spelling_survives_in_any_result_repr_or_log_record(mode):
    client, result, records = _run(mode)
    fields = " ".join(str(getattr(result, f.name)) for f in dataclasses.fields(result))
    surfaces = {
        "SendResult fields": fields,
        "SendResult repr": repr(result),
        "client repr": repr(client),
        "client str": str(client),
        "log records": record_blob(records),
    }
    for where, blob in surfaces.items():
        assert not leaked_forms(blob), f"{where} leaks {leaked_forms(blob)} (mode {mode})"


@pytest.mark.parametrize("mode", MODES)
def test_each_failure_mode_is_a_clean_non_ok_result(mode):
    _client, result, _records = _run(mode)
    assert result.ok is False
    assert result.kind in {"permanent", "transient"}
    assert result.message_id is None
    assert isinstance(result.detail, str) and result.detail


@pytest.mark.parametrize("mode", ["connect_error", "timeout", "non_json", "bad_json"])
def test_these_failure_modes_are_transient(mode):
    _client, result, _records = _run(mode)
    assert result.kind == "transient"


def test_the_httpx_request_line_is_logged_with_the_token_scrubbed_not_raw():
    """Control for the leak tests above: httpx DOES emit the URL at INFO, so a missing
    redactor would show up here (and an empty capture would make those tests meaningless)."""
    client, _ = make_client()
    with capture_logs() as logs:
        client.send_message("hello")
    httpx_records = [r for r in logs.records if r.name == "httpx"]
    assert httpx_records, "httpx logged no request line: the log-leak tests could not detect a leak"
    blob = record_blob(httpx_records)
    assert "api.telegram.org" in blob
    assert "REDACTED" in blob
    assert not leaked_forms(blob)


# ========================= the redactor on the httpx logger itself =====================
def _emit_every_spelling() -> None:
    """Log each token spelling the ways libraries do: plain message, %-args (also as an
    httpx.URL object), and inside an exception traceback."""
    log = logging.getLogger("httpx")
    for form in TOKEN_FORMS:
        log.debug("HTTP Request: POST https://api.telegram.org/%s/sendMessage", form)
        log.info(f"HTTP Request: POST https://api.telegram.org/{form}/sendMessage")
        log.warning("%s", form)
        try:
            raise httpx.ConnectError(f"All connection attempts failed for {form}")
        except httpx.ConnectError:
            log.exception("send failed")
    for url in (
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        f"https://api.telegram.org/bot{PCT}/sendMessage",
    ):
        log.info("HTTP Request: %s", httpx.URL(url))


def test_a_redaction_filter_is_attached_to_the_httpx_logger_itself():
    make_client()
    filters = logging.getLogger("httpx").filters
    assert any(isinstance(f, logging.Filter) or callable(f) for f in filters), filters


def test_records_logged_on_httpx_are_scrubbed_before_a_filterless_handler_sees_them():
    make_client()
    with capture_logs() as logs:
        _emit_every_spelling()
    blob = record_blob(logs.records)
    assert not leaked_forms(blob), leaked_forms(blob)


@given(
    pre=st.text(max_size=40),
    post=st.text(max_size=40),
    form=st.sampled_from(TOKEN_FORMS),
    style=st.sampled_from(["message", "args", "exception"]),
    level=st.sampled_from([logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR]),
)
@settings(max_examples=150, deadline=None, derandomize=True)
def test_no_token_spelling_survives_whatever_surrounds_it_in_a_log_record(
    pre, post, form, style, level
):
    telegram().TelegramClient(TOKEN, CHAT, http=make_http(Recorder()))
    text = f"{pre}{form}{post}"
    log = logging.getLogger("httpx")
    with capture_logs() as logs:
        if style == "message":
            log.log(level, text)
        elif style == "args":
            log.log(level, "request failed: %s", text)
        else:
            try:
                raise RuntimeError(text)
            except RuntimeError:
                log.log(level, "unexpected", exc_info=True)
    assert not leaked_forms(record_blob(logs.records))


def test_httpcore_stays_at_warning_or_above_even_under_a_debug_root_logger():
    """A fresh interpreter, so no earlier test (or setup_logging call) can have set the level
    for the client. Root is DEBUG, as in a developer's debugging session."""
    code = textwrap.dedent(
        f"""
        import json, logging, sys
        sys.path.insert(0, {str(ROOT / "src")!r})
        logging.getLogger().setLevel(logging.DEBUG)
        seen = []
        class H(logging.Handler):
            def emit(self, record):
                seen.append(record.name)
        logging.getLogger().addHandler(H())
        import onelot.notify.telegram as tg
        client = tg.TelegramClient({TOKEN!r}, {CHAT!r})
        for name in ("httpcore", "httpcore.connection", "httpcore.http11"):
            logging.getLogger(name).debug("probe request=%s", {TOKEN!r})
        print(json.dumps({{
            "httpcore": logging.getLogger("httpcore").getEffectiveLevel(),
            "httpcore_connection": logging.getLogger("httpcore.connection").getEffectiveLevel(),
            "httpcore_debug_records": [n for n in seen if n.startswith("httpcore")],
            "httpx_filters": len(logging.getLogger("httpx").filters),
        }}))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT
    )
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["httpcore"] >= logging.WARNING, out
    assert out["httpcore_connection"] >= logging.WARNING, out
    assert out["httpcore_debug_records"] == [], out
    assert out["httpx_filters"] >= 1, out


# ========================= what is registered with the redactor ========================
def test_the_three_token_forms_are_registered_with_the_redactor():
    make_client()
    literals = set(get_redactor()._literals)
    assert {TOKEN, SECRET, PCT} <= literals


def test_the_redactor_scrubs_every_spelling_of_the_token():
    make_client()
    scrub = get_redactor().scrub
    for text in (
        TOKEN,
        SECRET,
        PCT,
        f"POST /bot{TOKEN}/sendMessage",
        f"GET /bot{PCT}/getUpdates",
        f"key={SECRET};",
    ):
        scrubbed = scrub(text)
        assert not leaked_forms(scrubbed), (text, scrubbed)
        assert "REDACTED" in scrubbed


def test_the_chat_id_is_not_registered_and_ordinary_numbers_survive():
    make_client()
    redactor = get_redactor()
    assert CHAT not in redactor._literals
    line = f"update 100 chat {CHAT} offset 5 message_id 4711 ts 1762060000"
    assert redactor.scrub(line) == line


# ========================= detail is never built from exceptions or bodies =============
EXC_FACTORIES = {
    "connect": lambda req: httpx.ConnectError(MARKER, request=req),
    "read_timeout": lambda req: httpx.ReadTimeout(MARKER, request=req),
    "write_error": lambda req: httpx.WriteError(MARKER, request=req),
    "protocol": lambda req: httpx.RemoteProtocolError(MARKER, request=req),
    "decoding": lambda req: httpx.DecodingError(MARKER, request=req),
}


@pytest.mark.parametrize("name", sorted(EXC_FACTORIES))
def test_detail_never_contains_the_text_of_the_exception(name):
    def handler(request: httpx.Request) -> httpx.Response:
        raise EXC_FACTORIES[name](request)

    client, _ = make_client(handler)
    result = client.send_message("hello")
    assert result.kind == "transient"
    assert MARKER not in result.detail
    assert MARKER not in repr(result)
    assert result.detail


def test_detail_never_contains_the_text_of_an_http_status_error():
    client = telegram().TelegramClient(TOKEN, CHAT, http=_StatusErrorClient())
    result = client.send_message("hello")
    assert "Client error" not in result.detail  # that is str(exc), not status + description
    assert not leaked_forms(result.detail)


@pytest.mark.parametrize("status", [200, 400, 404, 429, 500, 502])
def test_detail_never_contains_a_non_json_response_body(status):
    client, _ = make_client(lambda r: httpx.Response(status, text=f"<html>{BODY_MARKER}</html>"))
    result = client.send_message("hello")
    assert BODY_MARKER not in result.detail
    assert result.detail


@pytest.mark.parametrize("status", [400, 404, 500])
def test_detail_uses_only_the_description_not_other_json_fields(status):
    client, _ = make_client(
        lambda r: tg_error(status, "the description", debug_dump=BODY_MARKER, parameters={"x": 1})
    )
    result = client.send_message("hello")
    assert "the description" in result.detail
    assert BODY_MARKER not in result.detail


@pytest.mark.parametrize("status", [401, 404, 500])
def test_a_telegram_description_that_echoes_the_token_is_scrubbed_from_detail(status):
    """Hardening beyond the literal AC wording: SendResult.detail contains Telegram's own text,
    and a proxy or a bad gateway can put the request path (with the token) in it."""
    echoes = [f"Not Found: /bot{TOKEN}/sendMessage", f"error for {PCT}", f"key {SECRET}"]
    for description in echoes:
        client, _ = make_client(lambda r, d=description: tg_error(status, d))
        with capture_logs() as logs:
            result = client.send_message("hello")
        assert not leaked_forms(result.detail), (status, leaked_forms(result.detail))
        assert not leaked_forms(repr(result))
        assert not leaked_forms(record_blob(logs.records))


# ========================= the client object itself ====================================
def test_repr_and_str_of_the_client_do_not_show_the_token_in_any_spelling():
    client, _ = make_client()
    for text in (repr(client), str(client), f"{client!r}", f"{client}", repr([client])):
        assert not leaked_forms(text)


def _data_attributes(obj: Any) -> dict[str, Any]:
    """Every non-callable, non-dunder attribute (works with __dict__ and __slots__ alike)."""
    found: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("__") and name.endswith("__"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:  # a property that raises is not a holder of the token
            continue
        if not callable(value):
            found[name] = value
    return found


def test_no_public_attribute_of_the_client_holds_the_token():
    client, _ = make_client()
    for name, value in _data_attributes(client).items():
        if not name.startswith("_"):
            assert not leaked_forms(repr(value) + str(value)), f"public attribute {name}"


def test_the_token_lives_in_a_private_attribute():
    client, _ = make_client()
    attrs = _data_attributes(client)
    private = {k: v for k, v in attrs.items() if k.startswith("_")}
    assert any(TOKEN in repr(v) or TOKEN in str(v) for v in private.values()), sorted(private)


def test_an_unavailable_client_with_a_token_shaped_bad_value_does_not_leak_it_either():
    bad = "123456789:AAH-" + "y" * 25  # a 29-character secret: one short of the 30 minimum
    client = telegram().TelegramClient(bad, CHAT, http=make_http(Recorder()))
    assert client.available is False
    result = client.send_message("hello")
    for blob in (repr(client), str(client), repr(result), str(client.unavailable_reason)):
        assert bad not in blob
        assert "y" * 25 not in blob
