"""US-003 mode B: attacks on TelegramClient, the redaction wiring and the Secrets hand-off.

Mode A pinned the contract per AC. These tests go after what is left: the REAL httpcore stack
(driven over an in-memory network backend, so no socket exists), status classes the AC does
not name, a wrong-typed message_id, how the redactor treats a wrongly shaped token, what the
client's own httpx.Client is built with, the lifecycle (close), and concurrent construction.
No test file contains a unicode escape: special characters come from chr() constants.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from us003_helpers import (
    CHAT,
    EMOJI_CHART,
    RUPEE,
    TOKEN,
    TOKEN_FORMS,
    Recorder,
    capture_logs,
    install_network_guard,
    json_response,
    leaked_forms,
    make_client,
    make_http,
    ok_response,
    record_blob,
    telegram,
)

from onelot.config import Secrets
from onelot.logging_setup import get_redactor, register_secrets

TEN_SECONDS = httpx.Timeout(10.0)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the client must only use an in-memory transport: {attempts}"


@pytest.fixture(autouse=True)
def _restore_logger_levels() -> Any:
    names = ("httpcore", "httpcore.connection", "httpcore.http11", "httpx")
    saved = {n: logging.getLogger(n).level for n in names}
    for name in names[:3]:  # start every test from "inherit", whatever ran before
        logging.getLogger(name).setLevel(logging.NOTSET)
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


# ==========================================================================================
# the real httpcore stack, no socket
# ==========================================================================================
class _RecordingStream(httpcore.MockStream):
    def __init__(self, buffer: list[bytes], sink: list[bytes]) -> None:
        super().__init__(buffer)
        self._sink = sink

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._sink.append(bytes(buffer))


class _RecordingBackend(httpcore.MockBackend):
    """httpcore's own mock backend, recording every byte the client writes."""

    def __init__(self, buffer: list[bytes], sink: list[bytes]) -> None:
        super().__init__(buffer)
        self._sink = sink

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        return _RecordingStream(list(self._buffer), self._sink)


def _wire_http(sink: list[bytes], message_id: int = 77) -> httpx.Client:
    """A real httpx.Client over the real httpcore connection pool and h11, talking to an
    in-memory server that answers every request with a Bot API success."""
    body = json.dumps({"ok": True, "result": {"message_id": message_id}}).encode()
    reply = (
        b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(network_backend=_RecordingBackend([reply], sink))
    return httpx.Client(transport=transport)


def test_the_real_httpcore_stack_would_log_at_debug_so_the_next_test_can_see_a_leak():
    """Control: with httpcore opened to DEBUG this very stack emits records. Without this the
    next test could pass only because nothing ever logs on this path."""
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    with capture_logs() as logs:
        _wire_http([]).post("https://api.telegram.org/bot-control/sendMessage", json={})
    assert [r for r in logs.records if r.name.startswith("httpcore")], "no httpcore DEBUG output"


def test_the_real_stack_logs_nothing_with_the_token_and_writes_it_only_on_the_request_line():
    # httpcore is at NOTSET here (the fixture), as in a fresh process: it would inherit the
    # DEBUG root that capture_logs installs, unless building the client raised it
    text = f"hello {EMOJI_CHART} {RUPEE} <b>x</b>"
    wire: list[bytes] = []
    client = telegram().TelegramClient(TOKEN, CHAT, http=_wire_http(wire))
    with capture_logs() as logs:
        result = client.send_message(text, silent=True)
    assert (result.ok, result.message_id) == (True, 77)
    assert not [r for r in logs.records if r.name.startswith("httpcore")]
    assert not leaked_forms(record_blob(logs.records))
    httpx_lines = [r.getMessage() for r in logs.records if r.name == "httpx"]
    assert any("api.telegram.org" in m and "REDACTED" in m for m in httpx_lines), httpx_lines
    # on the wire: one HTTP/1.1 POST, the token only in the request line
    raw = b"".join(wire)
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(f"POST /bot{TOKEN}/sendMessage HTTP/1.1".encode())
    assert b"host: api.telegram.org" in head.lower()
    assert TOKEN.encode() not in body
    assert int(head.lower().split(b"content-length: ")[1].split(b"\r\n")[0]) == len(body)
    payload = json.loads(body.decode("utf-8"))
    assert payload == {
        "chat_id": CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": True,
    }


def test_building_a_client_raises_httpcore_and_its_children_to_warning():
    for name in ("httpcore", "httpcore.connection", "httpcore.http11"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    telegram().TelegramClient(TOKEN, CHAT, http=make_http(Recorder()))
    for name in ("httpcore", "httpcore.connection", "httpcore.http11"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


def test_a_stricter_httpcore_level_is_kept():
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    logging.getLogger("httpcore.http11").setLevel(logging.CRITICAL)
    telegram().TelegramClient(TOKEN, CHAT, http=make_http(Recorder()))
    assert logging.getLogger("httpcore").level == logging.ERROR
    assert logging.getLogger("httpcore.http11").level == logging.CRITICAL


def test_many_clients_attach_the_redactor_to_the_httpx_logger_exactly_once():
    redactor = get_redactor()
    telegram().TelegramClient(TOKEN, CHAT, http=make_http(Recorder()))
    count = logging.getLogger("httpx").filters.count(redactor)
    for _ in range(40):
        telegram().TelegramClient(TOKEN, CHAT, http=make_http(Recorder()))
    assert count == 1
    assert logging.getLogger("httpx").filters.count(redactor) == 1


def test_a_real_setup_logging_run_writes_no_token_to_the_file_or_the_console(tmp_path):
    """The production path in a fresh interpreter: setup_logging with a rotating file and a
    console handler, a developer turning httpx request logging up to DEBUG, a send over the
    real httpcore stack (in memory), and a failure logged with its traceback."""
    code = textwrap.dedent(
        f"""
        import json, logging, sys
        sys.path.insert(0, {str(ROOT / "src")!r})
        import httpcore, httpx
        from onelot.logging_setup import setup_logging
        setup_logging(level="DEBUG", log_dir={str(tmp_path)!r}, secrets=[])
        from onelot.notify.telegram import TelegramClient
        body = json.dumps({{"ok": True, "result": {{"message_id": 5}}}}).encode()
        reply = (b"HTTP/1.1 200 OK\\r\\ncontent-length: " + str(len(body)).encode()
                 + b"\\r\\n\\r\\n" + body)
        transport = httpx.HTTPTransport()
        transport._pool = httpcore.ConnectionPool(
            network_backend=httpcore.MockBackend([reply]))
        token, chat = {TOKEN!r}, {CHAT!r}
        client = TelegramClient(token, chat, http=httpx.Client(transport=transport))
        logging.getLogger("httpx").setLevel(logging.DEBUG)  # request logging switched on
        result = client.send_message("hello")
        assert result.ok, result
        try:
            raise httpx.ConnectError("All connection attempts failed for "
                                     "https://api.telegram.org/bot" + token + "/sendMessage")
        except httpx.ConnectError:
            logging.getLogger("onelot.outbox").exception("send failed for bot%s", token)
        logging.getLogger("onelot.outbox").warning("token %s and %s", token.replace(":", "%3A"),
                                                   token.split(":")[1])
        for handler in logging.getLogger().handlers:
            handler.flush()
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    file_text = "\n".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.log*"))
    for where, blob in {"log file": file_text, "console": done.stdout + done.stderr}.items():
        assert not leaked_forms(blob), f"{where} leaks {leaked_forms(blob)}"
    assert "api.telegram.org" in file_text and "REDACTED" in file_text  # it was logged, masked


# ==========================================================================================
# how the client builds and owns its http client
# ==========================================================================================
def test_the_clients_own_http_client_verifies_tls_follows_no_redirect_and_times_out_at_ten_s(
    monkeypatch,
):
    seen: dict[str, Any] = {}
    original = httpx.Client.__init__

    def spy(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", spy)
    telegram().TelegramClient(TOKEN, CHAT)
    assert seen.get("verify", True) is not False
    assert not seen.get("follow_redirects", False)
    assert seen.get("timeout") == TEN_SECONDS


def test_a_redirect_is_not_followed_because_the_token_would_travel_to_another_host(monkeypatch):
    seen: list[httpx.Request] = []

    def redirect(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            302, headers={"location": "https://elsewhere.example/steal"}, request=request
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", redirect)
    result = telegram().TelegramClient(TOKEN, CHAT).send_message("hello")
    assert result.kind == "transient"
    assert [r.url.host for r in seen] == ["api.telegram.org"]


def test_close_closes_the_http_client_it_built_and_leaves_an_injected_one_alone(monkeypatch):
    closed: list[httpx.Client] = []
    monkeypatch.setattr(httpx.Client, "close", lambda self: closed.append(self))
    own = telegram().TelegramClient(TOKEN, CHAT)
    own.close()
    assert len(closed) == 1
    injected = make_http(Recorder())
    telegram().TelegramClient(TOKEN, CHAT, http=injected).close()
    assert len(closed) == 1  # the caller's client is the caller's to close


def test_an_unavailable_client_builds_no_http_client_and_close_is_harmless(monkeypatch):
    built: list[Any] = []
    original = httpx.Client.__init__

    def spy(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        built.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", spy)
    client = telegram().TelegramClient("bad", "bad")
    client.close()
    client.close()
    assert built == []


def test_sending_after_close_returns_a_result_instead_of_raising():
    """The class promises that every outcome comes back as a SendResult, never as an
    exception. After close() the client still says available=True, yet send_message raises
    RuntimeError('Cannot send a request, as the client has been closed.') out of the worker."""
    client = telegram().TelegramClient(TOKEN, CHAT)
    client.close()
    result = client.send_message("hello")
    assert result.ok is False
    assert result.kind in {"unavailable", "transient"}


# ==========================================================================================
# status classes and bodies the AC does not name
# ==========================================================================================
GOOD_BODY = {"ok": True, "result": {"message_id": 5}}


@pytest.mark.parametrize("status", [201, 202, 203, 206])
def test_other_2xx_with_a_good_body_is_a_successful_send(status):
    client, rec = make_client(lambda r: json_response(status, GOOD_BODY))
    result = client.send_message("x")
    assert (result.ok, result.kind, result.message_id) == (True, "ok", 5)
    assert rec.calls == 1


@pytest.mark.parametrize("status", [204, 205])
def test_a_2xx_with_no_body_is_transient_not_a_phantom_success(status):
    client, _ = make_client(lambda r: httpx.Response(status))
    assert client.send_message("x").kind == "transient"


@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308])
def test_3xx_is_transient_and_is_not_followed(status):
    client, rec = make_client(
        lambda r: httpx.Response(status, headers={"location": "https://elsewhere.example/"})
    )
    result = client.send_message("x")
    assert (result.ok, result.kind) == (False, "transient")
    assert rec.calls == 1


@pytest.mark.parametrize("status", [405, 409, 410, 413, 414, 415, 418, 422, 451])
def test_other_4xx_is_permanent(status):
    client, rec = make_client(lambda r: json_response(status, {"ok": False, "description": "no"}))
    assert client.send_message("x").kind == "permanent"
    assert rec.calls == 1


def test_408_request_timeout_is_transient():
    client, _ = make_client(lambda r: httpx.Response(408, text="timeout"))
    assert client.send_message("x").kind == "transient"


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 520, 522, 524, 599])
def test_every_5xx_is_transient(status):
    client, _ = make_client(lambda r: httpx.Response(status, text="down"))
    assert client.send_message("x").kind == "transient"


WRONG_MESSAGE_IDS = ["42", "", 42.0, 4.2e1, True, False, None, [42], {"id": 42}, 1e30]


@pytest.mark.parametrize("message_id", WRONG_MESSAGE_IDS, ids=[repr(m) for m in WRONG_MESSAGE_IDS])
def test_a_message_id_that_is_not_an_integer_is_transient_and_not_retried(message_id):
    body = {"ok": True, "result": {"message_id": message_id}}
    client, rec = make_client(lambda r: json_response(200, body))
    result = client.send_message("x")
    assert (result.ok, result.kind, result.message_id) == (False, "transient", None)
    assert rec.calls == 1  # the outbox decides whether to resend; the client never does


@pytest.mark.parametrize("message_id", [1, 4711, 2**31, 2**53, 2**63 + 5])
def test_any_integer_message_id_is_kept_exactly(message_id):
    client, _ = make_client(
        lambda r: json_response(200, {"ok": True, "result": {"message_id": message_id}})
    )
    assert client.send_message("x").message_id == message_id


@pytest.mark.parametrize("ok_value", [1, "true", "yes", [True], {"ok": True}, 0, None])
def test_only_a_boolean_true_ok_counts_as_success(ok_value):
    client, _ = make_client(
        lambda r: json_response(200, {"ok": ok_value, "result": {"message_id": 5}})
    )
    assert client.send_message("x").kind == "transient"


@pytest.mark.parametrize(
    "raw",
    [1e308, 3600, 86_400.5, 0.001, 10**18],
)
def test_a_large_retry_after_is_reported_as_a_float_not_clamped_or_slept(raw):
    body = {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": raw}}
    client, rec = make_client(lambda r: json_response(429, body))
    result = client.send_message("x")
    assert result.kind == "rate_limited"
    assert result.retry_after == float(raw)
    assert isinstance(result.retry_after, float)
    assert rec.calls == 1


@pytest.mark.parametrize("raw", [-1, -0.5, float("nan"), "5", True, None, [5], 10**400])
def test_an_unusable_retry_after_is_none_never_negative_or_nan(raw):
    text = json.dumps(
        {"ok": False, "parameters": {"retry_after": raw}}, allow_nan=True
    )  # nan becomes the JSON token NaN, which Python reads back
    client, _ = make_client(lambda r: httpx.Response(429, content=text.encode()))
    result = client.send_message("x")
    assert result.kind == "rate_limited"
    assert result.retry_after is None


def test_a_lone_surrogate_in_the_text_is_permanent_and_nothing_is_sent():
    client, rec = make_client()
    result = client.send_message("a" + chr(0xD800) + "b")
    assert (result.ok, result.kind, result.message_id) == (False, "permanent", None)
    assert rec.calls == 0
    assert client.send_message("fine").ok  # and the client is not left poisoned


def test_emoji_and_rupee_text_survive_the_trip_exactly():
    text = f"{EMOJI_CHART} {RUPEE}1,234.50 <b>x</b>\nline two"
    client, rec = make_client()
    assert client.send_message(text).ok
    assert json.loads(rec.requests[0].content.decode("utf-8"))["text"] == text


SECRETISH = st.sampled_from(TOKEN_FORMS).flatmap(
    lambda form: st.tuples(st.text(max_size=8), st.text(max_size=8)).map(
        lambda parts: parts[0] + form + parts[1]
    )
)
TEXT = st.one_of(st.text(max_size=300), SECRETISH)
SCALAR = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), TEXT)
JSON_LIKE = st.recursive(
    SCALAR,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(
            st.sampled_from(
                ["ok", "result", "message_id", "description", "parameters", "retry_after", "chat"]
            ),
            inner,
            max_size=5,
        ),
    ),
    max_leaves=14,
)


@given(
    status=st.integers(min_value=200, max_value=599),
    body=JSON_LIKE,
    raw=st.one_of(st.none(), st.binary(max_size=50)),
)
@settings(
    max_examples=400,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_any_status_and_body_gives_one_consistent_result_with_the_token_masked(status, body, raw):
    def respond(request: httpx.Request) -> httpx.Response:
        content = raw if raw is not None else json.dumps(body).encode()
        return httpx.Response(status, content=content)

    client, rec = make_client(respond)
    result = client.send_message("x")
    assert rec.calls == 1
    assert result.ok == (result.kind == "ok")
    assert (result.message_id is not None) == result.ok
    assert (result.retry_after is not None) <= (result.kind == "rate_limited")
    assert isinstance(result.detail, str) and len(result.detail) <= 260
    assert not leaked_forms(result.detail + repr(result))
    if status == 429:
        assert result.kind == "rate_limited"
    elif 400 <= status < 500 and status != 408:
        assert result.kind == "permanent"
    elif status >= 500 or 300 <= status < 400 or status == 408:
        assert result.kind == "transient"


# ==========================================================================================
# a wrongly shaped token and the redactor
# ==========================================================================================
def _emit(text: str) -> list[str]:
    """What a handler with no redaction filter receives when httpx logs `text`."""
    with capture_logs() as logs:
        logging.getLogger("httpx").info("HTTP Request: POST %s", text)
    return [r.getMessage() for r in logs.records]


# (case, raw value given to the client, the value without its ends, secret part >= 16 chars)
WRONG_SHAPE = {
    "five-digit-id": ("70003:Qw3rtyUiop9AsdfGhjk1ZxcvBnm2", True),
    "short-secret-part": ("700000001:abc", False),
    "padded-with-spaces-and-newline": ("  700000004:Lk8Lk8Lk8Lk8Lk8Lk8Lk8Lk8Lk8Lk8\n", True),
    "space-inside-the-secret": ("700000005:Mn7 Mn7 Mn7 Mn7 Mn7 Mn7", True),
    "twenty-char-secret": ("700000006:Pq5Pq5Pq5Pq5Pq5Pq5P", True),
}


@pytest.mark.parametrize("case", sorted(WRONG_SHAPE))
def test_a_wrongly_shaped_token_is_still_scrubbed_from_httpx_log_lines(case):
    raw, long_secret = WRONG_SHAPE[case]
    client = telegram().TelegramClient(raw, CHAT, http=make_http(Recorder()))
    assert client.available is False
    token = raw.strip()
    secret = token.partition(":")[2]
    shown = _emit(f"https://api.telegram.org/bot{token}/sendMessage")
    assert shown and token not in shown[0] and "REDACTED" in shown[0], shown
    if long_secret:
        assert secret not in _emit(f"echo {secret}")[0]
        assert secret not in _emit(token.replace(":", "%3A"))[0]


def test_a_short_secret_part_is_not_registered_on_its_own_so_ordinary_words_survive():
    telegram().TelegramClient("700000001:abc", CHAT, http=make_http(Recorder()))
    line = "the abc of logging, abcdef and cab"
    assert get_redactor().scrub(line) == line


CHAT_ID_SHAPED = ["-1007766554433", "4455667788", "9" * 19, "-5551234"]


@pytest.mark.parametrize("value", CHAT_ID_SHAPED)
def test_a_chat_id_pasted_into_the_token_field_is_not_registered_by_the_client(value):
    """It has no colon, so it is not token-like; registering it would scrub that number
    out of every log line (ADR-002 #8)."""
    telegram().TelegramClient(value, CHAT, http=make_http(Recorder()))
    line = f"update {value} offset 5"
    assert _emit(line)[0] == f"HTTP Request: POST {line}"
    assert get_redactor().scrub(line) == line


@pytest.mark.parametrize("value", CHAT_ID_SHAPED)
def test_a_chat_id_pasted_into_the_token_field_is_not_a_redaction_literal_via_secrets(
    value, monkeypatch, tmp_path
):
    """Same rule one level up: Secrets.secret_values() is what setup_logging and the engine
    register. The client skips a colon-less value for exactly this reason, but
    telegram_token_forms returns it whole, so the chat id is scrubbed from every log line
    after startup."""
    secrets = _load_secrets(monkeypatch, tmp_path, token=value)
    assert value not in secrets.secret_values()


def test_a_bare_secret_part_without_the_id_is_still_registered_by_secrets(monkeypatch, tmp_path):
    bare = "AAH-" + "Rt6Hy7Ju8Ik9Ol0P" * 2  # the secret pasted alone: not chat-id-shaped
    secrets = _load_secrets(monkeypatch, tmp_path, token=bare)
    assert bare in secrets.secret_values()


def test_secret_values_carry_all_three_spellings_of_a_good_token(monkeypatch, tmp_path):
    token = "800000007:AAH-" + "Vb2Nm3Lk4Jh5Gf6Dd7Ss8Aa9Qq0Ww1Ee"
    values = _load_secrets(monkeypatch, tmp_path, token=token).secret_values()
    secret = token.partition(":")[2]
    assert {token, token.replace(":", "%3A"), secret} <= set(values)
    register_secrets(values)
    for form in (token, secret, token.replace(":", "%3A")):
        assert form not in get_redactor().scrub(f"x {form} y")
    assert secret not in get_redactor().scrub(f"POST /bot{token}/sendMessage")


def _load_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str | None = None
) -> Secrets:
    for name in (
        "DHAN_CLIENT_ID",
        "DHAN_ACCESS_TOKEN",
        "DHAN_DATA_ACCESS_TOKEN",
        "DHAN_PIN",
        "DHAN_TOTP_SECRET",
        "ONELOT_API_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    if token is None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    return Secrets.load(project_root=tmp_path)


# ==========================================================================================
# concurrency: building and using clients from many threads at once
# ==========================================================================================
def test_concurrent_construction_and_sending_keeps_one_filter_and_correct_results():
    redactor = get_redactor()
    workers = 24
    start = threading.Barrier(workers, timeout=10)

    def work(i: int) -> list[Any]:
        token = f"9{i:08d}:AAH-" + f"Zy{i:02d}" * 9
        client, _ = make_client(lambda r, i=i: ok_response(5000 + i), token=token)
        start.wait()
        out = [client.send_message(f"m{i}-{n}") for n in range(3)]
        logging.getLogger("httpx").info("HTTP Request: POST /bot%s/sendMessage", token)
        return out

    with capture_logs() as logs, ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, range(workers)))
    for i, per_thread in enumerate(results):
        assert [r.message_id for r in per_thread] == [5000 + i] * 3
    assert logging.getLogger("httpx").filters.count(redactor) == 1
    blob = record_blob(logs.records)
    for i in range(workers):
        token = f"9{i:08d}:AAH-" + f"Zy{i:02d}" * 9
        assert token not in blob
        assert token.partition(":")[2] not in blob
