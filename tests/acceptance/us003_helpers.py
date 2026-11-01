"""Shared builders and checkers for the US-003 acceptance tests (Telegram client and formatter).

Nothing here touches production internals: the module under test is imported lazily through
``telegram()`` so that, until ``onelot.notify.telegram`` exists, every test FAILS at run time
with ModuleNotFoundError instead of the whole file erroring at collection.

The fixtures are real Desk dicts: ``stock_buy`` and friends mirror ``Desk._raise_signal`` and
``Economics.as_dict`` field for field (including the many fields the formatter must IGNORE),
and ``stock_position`` mirrors the dict ``Desk.acknowledge`` stores and ``_manage_positions``
sends as ``{"advice": ..., "position": ...}``.

All tokens are obviously fake. ``TOKEN`` has the shape of a real bot token on purpose.
"""

from __future__ import annotations

import copy
import datetime as dt
import html
import importlib
import json
import logging
import re
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from onelot.clock import IST

# -- fake credentials --------------------------------------------------------------------
TOKEN = "123456789:AAH-" + "x" * 35  # the AC4 shape: <9 digits>:AAH-<35 x>
SECRET = TOKEN.split(":", 1)[1]  # the part after the colon
PCT = TOKEN.replace(":", "%3A")  # the percent-encoded form
CHAT = "-1001234567890"
# every spelling of the token that must never be visible anywhere
TOKEN_FORMS = (TOKEN, f"bot{TOKEN}", SECRET, PCT, f"bot{PCT}")

# -- the clock every fixture is built around (a Monday, mid-session, IST) ----------------
NOW = dt.datetime(2026, 11, 2, 10, 27, 45, tzinfo=IST)
CREATED = "2026-11-02T10:27:03+05:30"
EXPIRES = "2026-11-02T10:37:03+05:30"  # 9 min 18 s after NOW


def telegram() -> ModuleType:
    """The module under test, imported at call time (see the module docstring)."""
    return importlib.import_module("onelot.notify.telegram")


# ========================= alert fixtures (Desk._raise_signal shape) ====================
def _stock_economics(side: str, entry: float, stop: float, target: float | None) -> dict[str, Any]:
    return {
        "kind": "STOCK",
        "side": side,
        "qty": 1,
        "unit_label": "1 share",
        "underlying_entry": entry,
        "underlying_stop": stop,
        "underlying_target": target,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "outlay": entry,
        "risk_inr": 8.91,
        "reward_inr": 13.88 if target else None,
        "costs_inr": 1.52,
        "rr_net": 1.56 if target else None,
        "r_target": 2.0 if target else 0.0,
        "option": {},
        "synthetic": False,
        "notes": [],
    }


def _index_economics(
    side: str,
    opt: str,
    strike: float,
    lot: int,
    synthetic: bool,
    und: tuple[float, float, float | None],
    prem: tuple[float, float, float | None],
    expiry: str = "2026-11-26",
) -> dict[str, Any]:
    option: dict[str, Any] = {
        "strike": strike,
        "opt_type": opt,
        "expiry": expiry,
        "delta": 0.45,
        "iv": 0.1432,
        "premium": prem[0],
        "lot": lot,
    }
    if not synthetic:  # the live-chain builder adds the quote and the chain row
        option.update({"bid": prem[0] - 0.5, "ask": prem[0], "spread_pct": 0.4, "oi": 183000})
        option["security_id"] = "55012"
    return {
        "kind": "INDEX",
        "side": side,
        "qty": lot,
        "unit_label": f"1 lot ({lot})",
        "underlying_entry": und[0],
        "underlying_stop": und[1],
        "underlying_target": und[2],
        "entry_price": prem[0],
        "stop_price": prem[1],
        "target_price": prem[2],
        "outlay": round(prem[0] * lot, 2),
        "risk_inr": 912.4,
        "reward_inr": 1021.0 if prem[2] else None,
        "costs_inr": 71.3,
        "rr_net": 1.12 if prem[2] else None,
        "r_target": 2.0 if prem[2] else 0.0,
        "option": option,
        "synthetic": synthetic,
        "notes": ["live chain quote" if not synthetic else "premium is a Black-76 estimate"],
    }


def _alert(**fields: Any) -> dict[str, Any]:
    """The Desk signal dict, including every field the formatter has to ignore."""
    base: dict[str, Any] = {
        "id": "a1b2c3d4e5f6",
        "created_at": CREATED,
        "expires_at": EXPIRES,
        "bar_ts": "2026-11-02T10:25:00+05:30",
        "strength": 0.7,
        "horizon": "intraday",
        "reasons": ["RSI(14) 24.1 < 30", "close below lower band"],
        "text": "BUY 1 share ... Why: RSI(14) 24.1 < 30",
        "status": "PENDING",
        "risk_allowed": True,
        "risk_reason": "ok",
        "outcome": None,
        "manage": {
            "target_r": 2.0,
            "trail_atr_mult": 0.0,
            "trail_activate_r": 1.0,
            "max_bars": 0,
            "exit_time": "15:15",
            "atr": 6.1,
            "tf_min": 5,
            "horizon": "intraday",
        },
    }
    base.update(fields)
    return base


def stock_buy() -> dict[str, Any]:
    """Golden fixture 1: stock BUY, live Dhan feed, fixed target."""
    return _alert(
        instrument_key="SBIN",
        series_key="SBIN",
        label="State Bank of India",
        kind="STOCK",
        side="BUY",
        direction="long",
        strategy="mean_reversion",
        strategy_label="Trend-filtered Mean Reversion",
        timeframe="5m",
        confidence={
            "score": 71.6,
            "label": "high",
            "parts": {"signal_strength": 70.0, "regime_fit": 70.0},
        },
        data={
            "source": "dhan",
            "resolution": "1m",
            "delayed": False,
            "delay_note": "live exchange feed",
            "bar_age_sec": 12.0,
        },
        economics=_stock_economics("BUY", 812.4, 805.0, 827.2),
    )


def stock_sell() -> dict[str, Any]:
    """Golden fixture 2: stock SELL (short, intraday) served by the Yahoo fallback (delayed)."""
    a = _alert(
        id="0f9e8d7c6b5a",
        created_at="2026-11-02T11:03:41+05:30",
        expires_at="2026-11-02T11:13:41+05:30",
        instrument_key="RELIANCE",
        series_key="RELIANCE",
        label="Reliance Industries",
        kind="STOCK",
        side="SELL",
        direction="short",
        strategy="ema_cross",
        strategy_label="EMA Cross (trend + momentum + volume)",
        timeframe="15m",
        confidence={"score": 58.4, "label": "medium", "parts": {}},
        data={
            "source": "yahoo",
            "resolution": "2m",
            "delayed": True,
            "delay_note": "Yahoo bars run ~15 minutes behind the exchange",
            "bar_age_sec": 540.0,
        },
        economics=_stock_economics("SELL", 2951.35, 2967.10, 2919.85),
    )
    return a


NOW_SELL = dt.datetime(2026, 11, 2, 11, 10, 0, tzinfo=IST)  # 3 min 41 s before the deadline


def index_ce_live() -> dict[str, Any]:
    """Golden fixture 3: index CE, premium from the live Dhan option chain (ask)."""
    return _alert(
        id="c0ffee123456",
        instrument_key="BANKNIFTY_OPT",
        series_key="BANKNIFTY",
        label="NIFTY BANK",
        kind="INDEX",
        side="BUY_CE",
        direction="long",
        strategy="ema_cross",
        strategy_label="EMA Cross (trend + momentum + volume)",
        timeframe="12m",
        confidence={"score": 68.2, "label": "medium", "parts": {}},
        data={
            "source": "dhan",
            "resolution": "1m",
            "delayed": False,
            "delay_note": "live exchange feed",
            "bar_age_sec": 8.0,
        },
        economics=_index_economics(
            "BUY_CE",
            "CE",
            52000.0,
            30,
            synthetic=False,
            und=(52010.5, 51850.0, 52300.0),
            prem=(125.5, 98.0, 160.0),
        ),
    )


def index_pe_synthetic() -> dict[str, Any]:
    """Golden fixture 4: index PE, premium is the Black-76 estimate (synthetic)."""
    return _alert(
        id="5e1f00abcdef",
        created_at="2026-11-02T13:12:09+05:30",
        expires_at="2026-11-02T13:22:09+05:30",
        instrument_key="NIFTY_OPT",
        series_key="NIFTY",
        label="NIFTY 50",
        kind="INDEX",
        side="BUY_PE",
        direction="short",
        strategy="supertrend_vol",
        strategy_label="Supertrend (tuned) + volume + momentum",
        timeframe="5m",
        confidence={"score": 44.9, "label": "low", "parts": {}},
        data={
            "source": "dhan",
            "resolution": "1m",
            "delayed": False,
            "delay_note": "live exchange feed",
            "bar_age_sec": 21.0,
        },
        economics=_index_economics(
            "BUY_PE",
            "PE",
            24500.0,
            65,
            synthetic=True,
            und=(24510.25, 24610.0, 24320.5),
            prem=(118.35, 92.1, 150.45),
            expiry="2026-12-03",
        ),
    )


NOW_PE = dt.datetime(2026, 11, 2, 13, 15, 30, tzinfo=IST)  # 6 min 39 s before the deadline


def no_target_stock() -> dict[str, Any]:
    """Golden fixture 5a: the stock BUY of fixture 1 with no fixed target (trail instead)."""
    a = stock_buy()
    eco = a["economics"]
    eco.update(underlying_target=None, target_price=None, reward_inr=None, rr_net=None)
    eco["r_target"] = 0.0
    a["manage"]["target_r"] = 0.0
    return a


def no_target_index() -> dict[str, Any]:
    """Golden fixture 5b: the index CE of fixture 3 with no fixed target."""
    a = index_ce_live()
    eco = a["economics"]
    eco.update(underlying_target=None, target_price=None, reward_inr=None, rr_net=None)
    eco["r_target"] = 0.0
    return a


# ========================= EXIT fixtures ({advice, position}) ===========================
def _position(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "e1d2c3b4a5f6",
        "signal_id": "a1b2c3d4e5f6",
        "opened_at": "2026-11-02T10:28:00+05:30",
        "horizon": "intraday",
        "trail_active": False,
        "bars_held": 3,
        "sessions_held": 1,
        "exit_advice": None,
        "hold_check": "strategy mean_reversion 5m: still long - HOLD",
        "status": "OPEN",
        "source": "dashboard",
        "notes": [],
        "manage": {"target_r": 2.0, "exit_time": "15:15", "tf_min": 5, "horizon": "intraday"},
        "confidence": {"score": 71.6, "label": "high", "parts": {}},
    }
    base.update(fields)
    return base


def stock_position(**over: Any) -> dict[str, Any]:
    p = _position(
        instrument_key="SBIN",
        series_key="SBIN",
        label="State Bank of India",
        kind="STOCK",
        side="BUY",
        direction="long",
        strategy="mean_reversion",
        timeframe="5m",
        qty=1,
        entry_price=812.4,
        entry_underlying=812.4,
        stop_underlying=805.0,
        target_underlying=827.2,
        initial_stop_underlying=805.0,
        option={},
        last_underlying=805.0,
        unrealized=-7.4,
        r_now=-1.0,
    )
    p.update(over)
    return p


def index_position(**over: Any) -> dict[str, Any]:
    p = _position(
        instrument_key="BANKNIFTY_OPT",
        series_key="BANKNIFTY",
        label="NIFTY BANK",
        kind="INDEX",
        side="BUY_CE",
        direction="long",
        strategy="ema_cross",
        timeframe="12m",
        qty=30,
        entry_price=125.5,
        entry_underlying=52010.5,
        stop_underlying=51850.0,
        target_underlying=52300.0,
        initial_stop_underlying=51850.0,
        option={"strike": 52000.0, "opt_type": "CE", "expiry": "2026-11-26", "lot": 30},
        last_underlying=52301.2,
        unrealized=1234.56,
        r_now=1.81,
    )
    p.update(over)
    return p


def index_pe_position(**over: Any) -> dict[str, Any]:
    p = index_position(
        instrument_key="NIFTY_OPT",
        series_key="NIFTY",
        label="NIFTY 50",
        side="BUY_PE",
        direction="short",
        qty=65,
        entry_price=118.35,
        option={"strike": 24500.0, "opt_type": "PE", "expiry": "2026-12-03", "lot": 65},
        unrealized=310.2,
        trail_active=True,
        r_now=0.9,
    )
    p.update(over)
    return p


def exit_event(position: Mapping[str, Any], advice: str) -> dict[str, Any]:
    return {"advice": advice, "position": dict(position)}


# ========================= HTML checkers (Telegram HTML parse mode) =====================
ALLOWED_TAGS = frozenset({"b", "i", "code"})
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)>")
_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|#[0-9]+|#[xX][0-9a-fA-F]+);")
DOT = chr(0x00B7)  # the separator between header parts
ELLIPSIS = chr(0x2026)  # the single-character ellipsis
ELLIPSES = (ELLIPSIS, "...")  # AC1 says "an ellipsis": either spelling is accepted
RUPEE = chr(0x20B9)
ZWJ = chr(0x200D)
E_ACUTE = chr(0x00E9)
EMOJI_CHART = chr(0x1F4C8)
EMOJI_ROCKET = chr(0x1F680)
EMOJI_FAMILY = ZWJ.join(chr(c) for c in (0x1F468, 0x1F469, 0x1F467))


def fullwidth(digits: str) -> str:
    """ASCII digits and '-' rewritten as full-width forms (U+FF10..U+FF19, U+FF0D)."""
    return "".join(chr(0xFF0D) if c == "-" else chr(0xFF10 + int(c)) for c in digits)


def arabic_indic(digits: str) -> str:
    """ASCII digits rewritten as Arabic-Indic digits (U+0660..U+0669)."""
    return "".join(chr(0x0660 + int(c)) for c in digits)


def tags_in(text: str) -> list[tuple[bool, str]]:
    return [(bool(m.group(1)), m.group(2).lower()) for m in _TAG.finditer(text)]


def html_problems(text: str) -> list[str]:
    """Everything wrong with `text` as Telegram HTML: [] means it is well formed.

    Telegram accepts only a few tags; every other `<`, `>` and `&` must be an entity. A
    truncation in the middle of an entity (`&am`) or a tag (`<b`) shows up here.
    """
    problems: list[str] = []
    stack: list[str] = []
    for closing, name in tags_in(text):
        if name not in ALLOWED_TAGS:
            problems.append(f"disallowed tag <{name}>")
        elif not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            problems.append(f"unbalanced </{name}>")
    if stack:
        problems.append(f"unclosed tags {stack}")
    bare = _TAG.sub("", text)
    if "<" in bare or ">" in bare:
        problems.append("a raw < or > outside a tag")
    for m in re.finditer(r"&", bare):
        if not _ENTITY.match(bare, m.start()):
            problems.append(f"raw or truncated & at {bare[m.start() : m.start() + 8]!r}")
            break
    return problems


def assert_budget(text: str, *, max_lines: int = 12, max_chars: int = 600) -> None:
    lines = text.split("\n")
    assert len(lines) <= max_lines, f"{len(lines)} lines > {max_lines}: {text!r}"
    assert len(text) <= max_chars, f"{len(text)} chars > {max_chars}: {text!r}"


def assert_well_formed(text: str) -> None:
    problems = html_problems(text)
    assert not problems, f"{problems} in {text!r}"
    text.encode("utf-8")  # no lone surrogates from a careless slice


def visible(text: str) -> str:
    """The text a phone would show: tags dropped, entities decoded."""
    return html.unescape(_TAG.sub("", text))


def squash(s: str) -> str:
    """Whitespace runs to single spaces, ends trimmed (what 'newlines collapsed' must yield)."""
    return " ".join(s.split())


def header_of(text: str) -> str:
    return text.split("\n", 1)[0]


# ========================= transport doubles ============================================
def json_response(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def ok_response(message_id: int = 42) -> httpx.Response:
    return json_response(200, {"ok": True, "result": {"message_id": message_id, "text": "t"}})


def tg_error(status: int, description: str, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    body.update(extra)
    return json_response(status, body)


class Recorder:
    """A thread-safe MockTransport handler that records every request it serves."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()
        self._respond = respond or (lambda request: ok_response())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        return self._respond(request)

    @property
    def calls(self) -> int:
        with self._lock:
            return len(self.requests)


def make_http(
    handler: Callable[[httpx.Request], httpx.Response], *, timeout: float = 3.0
) -> httpx.Client:
    """A real httpx.Client over a MockTransport. Its OWN default timeout differs from 10 s,
    so only a per-request timeout set by the client under test can satisfy AC3."""
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)


def make_client(
    respond: Callable[[httpx.Request], httpx.Response] | Recorder | None = None,
    *,
    token: str = TOKEN,
    chat_id: str = CHAT,
) -> tuple[Any, Recorder]:
    rec = respond if isinstance(respond, Recorder) else Recorder(respond)
    return telegram().TelegramClient(token, chat_id, http=make_http(rec)), rec


def payload_of(request: httpx.Request) -> dict[str, Any]:
    """The sendMessage parameters however they were encoded (JSON body, form body or query)."""
    params: dict[str, Any] = dict(request.url.params)
    body = request.content
    if body:
        ctype = request.headers.get("content-type", "")
        if "json" in ctype:
            decoded = json.loads(body)
            assert isinstance(decoded, dict)
            params.update(decoded)
        else:
            params.update({k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()})
    return params


def truthy_flag(value: Any) -> bool:
    return value is True or str(value).lower() in ("true", "1")


# ========================= network guard (AC6) ==========================================
class NetworkBlocked(AssertionError):
    """Raised by the guard when anything in the suite tries to reach the real network."""


def install_network_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every outbound connection attempt fail loudly and record it. Returns the list of
    attempts; the autouse fixture in each module asserts it is empty after every test."""
    attempts: list[str] = []

    def blocked(what: str) -> Callable[..., Any]:
        def guard(*args: Any, **kwargs: Any) -> Any:
            attempts.append(what)
            raise NetworkBlocked(f"US-003 tests must not use the network ({what})")

        return guard

    monkeypatch.setattr(socket, "create_connection", blocked("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", blocked("socket.gethostbyname"))
    for name in ("connect", "connect_ex"):
        original = getattr(socket.socket, name)

        def guarded(
            self: socket.socket,
            address: Any,
            *,
            _orig: Callable[..., Any] = original,
            _name: str = name,
        ) -> Any:
            if self.family == socket.AF_UNIX:  # local IPC is not "the network"
                return _orig(self, address)
            attempts.append(f"socket.{_name}")
            raise NetworkBlocked(f"US-003 tests must not use the network (socket.{_name})")

        monkeypatch.setattr(socket.socket, name, guarded)
    return attempts


# ========================= log capture WITHOUT the global filter (AC4) ==================
class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_logs() -> Iterator[_ListHandler]:
    """Attach a bare handler (NO redaction filter, as in a test that does not call
    setup_logging) to the root logger and open the httpx logger to DEBUG.

    Call it AFTER the TelegramClient is constructed: anything the client does to logger levels
    at construction time must not be able to hide the leak this test is looking for.
    """
    handler = _ListHandler()
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    saved = (root.level, httpx_logger.level)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    httpx_logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(saved[0])
        httpx_logger.setLevel(saved[1])


def record_blob(records: list[logging.LogRecord]) -> str:
    """Everything a handler, a formatter or a JSON log shipper could see in the records."""
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    parts: list[str] = []
    for r in records:
        try:
            parts.append(r.getMessage())
        except Exception as exc:  # a broken record is itself worth showing
            parts.append(f"<unrenderable record: {type(exc).__name__}>")
        parts.extend((repr(r.msg), repr(r.args), r.exc_text or ""))
        try:
            parts.append(fmt.format(r))  # renders exc_info (the traceback) too
        except Exception as exc:
            parts.append(f"<unformattable record: {type(exc).__name__}>")
    return "\n".join(parts)


def leaked_forms(blob: str) -> list[str]:
    """Which of the token spellings occur in `blob` (labels only, never the values)."""
    names = ("full token", "bot+token", "part after the colon", "%3A form", "bot+%3A form")
    return [n for n, form in zip(names, TOKEN_FORMS, strict=True) if form in blob]


def deep_copy(alert: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(alert))


def label_shown(text: str, label: str) -> tuple[bool, int]:
    """Where did the label end up? (True, n) when the whole label is visible anywhere in the
    message; (False, k) when only its first k characters survive, followed by an ellipsis;
    (False, 0) when neither. Layout-free, so it holds whichever line carries the label."""
    shown = squash(visible(text))
    want = squash(label)
    if want in shown:
        return True, len(want)
    for k in range(len(want) - 1, 0, -1):
        if any(want[:k] + e in shown for e in ELLIPSES):
            return False, k
    return False, 0
