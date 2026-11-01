"""Telegram phone messages for Desk alerts (ADR-002, US-003).

The formatters turn a Desk payload into Telegram HTML that the owner can act on from
the lock screen. The first line names exactly what to type into Dhan: the tradable
symbol (series_key), the contract and the unit ("BUY BANKNIFTY 25 NOV 52000 CE · 1 lot
(30)"); a risk-blocked alert is still sent, with "[BLOCKED by risk] " first inside the
bold. The rest follows the AC1 order:

    <b>{BUY|SELL} {series_key}[ {DD MMM} {strike} {CE|PE}] · {1 share | 1 lot (N)}</b>
    <i>{label}</i>                          omitted when it repeats the symbol
    Entry {price} {mkt | ask | est.}        stock: mkt; index: ask (live chain) | est. (synthetic)
    Underlying Stop {x} · Target {y|trail}  index only
    Stop {x} · Target {y|trail}             stock: share prices; index: premium levels
    {strategy label} {timeframe}
    Conf {NN} {label}
    Valid till {HH:MM} IST ({M}m left | expired)
    Data {live | <b>delayed ~15m</b> | n/a} data.delayed only (US-028); n/a unless a bool
    Alert {HH:MM:SS} IST · id <code>{first six}</code>

Rules every formatter keeps:
  * a WHITELIST of fields is read and every other key is ignored, so account data
    (balances, holdings, ledger, tokens) can never leave the machine; only scalar
    values are rendered, a container is never stringified;
  * every dynamic value is HTML-escaped and collapsed to one line;
  * only <b>, <i> and <code> tags, always balanced;
  * at most MAX_LINES lines and MAX_CHARS characters. Every field except the
    instrument label has a fixed cap in HTML characters, so the budget always holds;
    the label takes whatever room is left and is cut with an ellipsis, never inside
    an entity or a tag.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import math
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

import httpx

from ..clock import to_ist
from ..config import telegram_redaction_literals
from ..logging_setup import get_redactor, register_secrets
from .base import shown_confidence

log = logging.getLogger(__name__)

MAX_LINES = 12
MAX_CHARS = 600
ELLIPSIS = "…"
SEP = " · "
NA = "n/a"
DELAYED_TAG = "delayed ~15m"
BLOCKED_PREFIX = "[BLOCKED by risk] "  # the same words as the desk notification title
ID_CHARS = 6
# English and upper case from a table: strftime("%b") follows the process locale.
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# Caps in HTML characters after escaping; with these the fixed part of the longest
# message (a blocked index alert) stays well under MAX_CHARS whatever the input
# (test_the_fixed_part_leaves_the_label_room pins the worst case).
_CAP_SYMBOL = 32
_CAP_ACTION = 12  # an unknown side shown as the action word
_CAP_STRATEGY = 48
_CAP_TIMEFRAME = 24
_CAP_CONF_LABEL = 24
_CAP_REASON = 48
_MAX_ABS_NUMBER = Decimal("1e12")  # no price, level or P&L here is a trillion rupees
_CENT = Decimal("0.01")
_UNIT = Decimal(1)

_OPTION_SIDES = ("BUY_CE", "BUY_PE")
# C0/C1 controls that are not whitespace; bidi marks and overrides that could re-order a
# line (U+061C, U+200E/F, U+202A-E, U+2066-9); the byte-order mark U+FEFF; and lone
# surrogates U+D800-DFFF, which cannot be encoded, so one would make the alert unsendable.
_INVISIBLE = re.compile(
    "[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069"
    "\ufeff\ud800-\udfff]"
)


# ── scalar helpers ────────────────────────────────────────────────────────────
def _fields(value: Any) -> Mapping[str, Any]:
    """`value` when it is a mapping, else an empty one (malformed payloads never raise)."""
    return value if isinstance(value, Mapping) else {}


def _plain(value: Any) -> str:
    """One line of text from a scalar: whitespace runs (newlines included) become one
    space, invisible controls go. Containers and None give "" — never their repr."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, Decimal)):
        text = str(value)
    else:
        return ""
    return " ".join(_INVISIBLE.sub("", text).split())


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _fit(value: Any, limit: int) -> str:
    """Escaped one-line text of at most `limit` HTML characters. A cut happens on the
    plain text, before escaping, so it can never split an entity; it ends in an ellipsis."""
    text = _plain(value)
    escaped = _esc(text)
    if len(escaped) <= limit:
        return escaped
    pieces: list[str] = []
    used = 0
    for ch in text:
        piece = _esc(ch)
        if used + len(piece) > limit - 1:
            break
        pieces.append(piece)
        used += len(piece)
    return ("".join(pieces).rstrip() + ELLIPSIS) if limit >= 1 else ""


def _decimal(value: Any) -> Decimal | None:
    """A finite, sane number, or None for missing, garbage, NaN, inf or absurd values."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or abs(number) >= _MAX_ABS_NUMBER:
        return None
    return number


def _rounded(value: Any, step: Decimal) -> Decimal | None:
    """Rounded half-up (2.675 -> 2.68, unlike binary-float formatting)."""
    number = _decimal(value)
    return None if number is None else number.quantize(step, rounding=ROUND_HALF_UP)


def _num(value: Any) -> str:
    """2 decimals, comma-grouped: 2,951.35."""
    q = _rounded(value, _CENT)
    return NA if q is None else f"{'-' if q < 0 else ''}{abs(q):,.2f}"


def _whole_signed(value: Any) -> str:
    """Whole rupees with a sign: +1,235 / -7 / 0."""
    q = _rounded(value, _UNIT)
    if q is None:
        return NA
    return f"{'+' if q > 0 else '-' if q < 0 else ''}{abs(q):,.0f}"


def _strike(value: Any) -> str:
    """52000.0 -> 52000, 22.5 -> 22.5. The strike is typed into Dhan, so anything that is
    not a finite number (nan, inf, a bool, free text, missing) shows '?', never a word."""
    q = _rounded(value, _CENT)
    return "?" if q is None else f"{q.normalize():f}"


def _when(value: Any) -> dt.datetime | None:
    """An aware IST datetime from a datetime or an ISO string (naive means IST)."""
    if isinstance(value, dt.datetime):
        return to_ist(value)
    if isinstance(value, str):
        try:
            return to_ist(dt.datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _clock(moment: dt.datetime | None, pattern: str) -> str:
    return f"{moment.strftime(pattern)} IST" if moment is not None else NA


def _expiry(value: Any) -> str:
    """'03 DEC' — the expiry day (two digits) and month, read in IST — from a date, a
    datetime or an ISO string; "" when missing or unreadable, so it is simply left out."""
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        day: dt.date | None = value
    else:
        moment = _when(value.strip() if isinstance(value, str) else value)
        day = moment.date() if moment is not None else None
    return "" if day is None else f"{day.day:02d} {_MONTHS[day.month - 1]}"


def _short_id(value: Any) -> str:
    return f"id <code>{_esc(_plain(value)[:ID_CHARS])}</code>"


# ── shared pieces ────────────────────────────────────────────────────────────
def _instrument(payload: Mapping[str, Any], option: Mapping[str, Any]) -> tuple[str, str]:
    """(action word, instrument) for an alert or a position, both escaped: the tradable
    symbol, plus expiry, strike and CE/PE for an option. The CE/PE comes from the Desk
    side ("BUY_CE"), never from free text."""
    side = _plain(payload.get("side")).upper()
    symbol = _fit(payload.get("series_key"), _CAP_SYMBOL) or "?"
    if side in _OPTION_SIDES:
        contract = (_expiry(option.get("expiry")), _strike(option.get("strike")), side[-2:])
        return "BUY", " ".join([symbol, *(part for part in contract if part)])
    if side in ("BUY", "SELL"):
        return side, symbol
    return (_fit(side.replace("_", " "), _CAP_ACTION) or "?"), symbol


def _unit(kind: str, economics: Mapping[str, Any]) -> str:
    """Derived from the kind and the lot size, never from free text ("" if unknown)."""
    if kind == "STOCK":
        return "1 share"
    if kind != "INDEX":
        return ""
    lot = _rounded(economics.get("qty"), _UNIT) or _rounded(
        _fields(economics.get("option")).get("lot"), _UNIT
    )
    return f"1 lot ({lot:f})" if lot is not None and lot > 0 else "1 lot"


def _header(alert: Mapping[str, Any]) -> str:
    """'BUY BANKNIFTY 25 NOV 45000 CE · 1 lot (30)' — escaped, without tags."""
    economics = _fields(alert.get("economics"))
    action, instrument = _instrument(alert, _fields(economics.get("option")))
    kind = _plain(alert.get("kind")).upper()
    unit = _unit(kind, economics)
    return f"{action} {instrument}{SEP}{unit}" if unit else f"{action} {instrument}"


def _levels(head: str, stop: Any, target: Any) -> str:
    """'{head} {stop} · Target {target}', or 'Target trail' when there is none."""
    shown = "trail" if target is None else _num(target)
    return f"{head} {_num(stop)}{SEP}Target {shown}"


# ── format_alert ──────────────────────────────────────────────────────────────
def format_alert(alert: Mapping[str, Any], now: dt.datetime) -> str:
    """The phone message for a Desk SIGNAL dict (see `Desk._raise_signal`).

    Reads only: id, created_at, expires_at, series_key, label, kind, side, risk_allowed,
    strategy_label (else strategy), timeframe, confidence.{score,label}, data.delayed and
    economics.{qty, entry_price, stop_price, target_price, underlying_stop,
    underlying_target, synthetic, option.{strike, expiry, lot}}. `now` drives the minutes
    left, so format at send time. Only risk_allowed exactly False marks it blocked.
    """
    economics = _fields(alert.get("economics"))
    index = _plain(alert.get("kind")).upper() == "INDEX"
    if not index:
        entry_tag = "mkt"
    else:  # a live-chain premium is the ask you pay; anything else is a model estimate
        entry_tag = "ask" if economics.get("synthetic") is False else "est."
    blocked = BLOCKED_PREFIX if alert.get("risk_allowed") is False else ""
    lines = [
        f"<b>{blocked}{_header(alert)}</b>",
        f"Entry {_num(economics.get('entry_price'))} {entry_tag}",
    ]
    if index:
        lines.append(
            _levels(
                "Underlying Stop",
                economics.get("underlying_stop"),
                economics.get("underlying_target"),
            )
        )
    lines += [
        _levels("Stop", economics.get("stop_price"), economics.get("target_price")),
        _strategy_line(alert),
        _confidence_line(_fields(alert.get("confidence"))),
        _ttl_line(_when(alert.get("expires_at")), to_ist(now)),
        _data_line(_fields(alert.get("data"))),
        f"Alert {_clock(_when(alert.get('created_at')), '%H:%M:%S')}{SEP}"
        + _short_id(alert.get("id")),
    ]
    _insert_label(lines, alert)
    return "\n".join(lines)


def _strategy_line(alert: Mapping[str, Any]) -> str:
    name = alert.get("strategy_label") or alert.get("strategy")
    parts = (_fit(name, _CAP_STRATEGY), _fit(alert.get("timeframe"), _CAP_TIMEFRAME))
    return " ".join(part for part in parts if part) or f"Strategy {NA}"


def _confidence_line(confidence: Mapping[str, Any]) -> str:
    """'Conf 72 high': the same floored number as the desk title (shown_confidence)."""
    score = shown_confidence(confidence.get("score"))
    shown = NA if score is None else str(score)
    label = _fit(confidence.get("label"), _CAP_CONF_LABEL)
    return f"Conf {shown} {label}" if label else f"Conf {shown}"


def _ttl_line(deadline: dt.datetime | None, now: dt.datetime) -> str:
    """Floored minutes; 'expired' once they reach 0, so the last 59 s read expired too
    (AC1: 'expired when 0 or less' — a stale alert is never offered as actionable)."""
    if deadline is None:
        return f"Valid till {NA}"
    minutes = int((deadline - now).total_seconds() // 60)
    left = f"{minutes}m left" if minutes > 0 else "expired"
    return f"Valid till {_clock(deadline, '%H:%M')} ({left})"


def _data_line(data: Mapping[str, Any]) -> str:
    """US-028: data.delayed is the single rule; data.source is never re-derived here.
    Fail-safe: 'live' only for exactly False, so unknown freshness reads n/a, never live."""
    delayed = data.get("delayed")
    if delayed is True:
        return f"Data <b>{DELAYED_TAG}</b>"
    return "Data live" if delayed is False else f"Data {NA}"


def _insert_label(lines: list[str], alert: Mapping[str, Any]) -> None:
    """Put the instrument label under the header, in the room the budget leaves."""
    label = _plain(alert.get("label"))
    if not label or label == _plain(alert.get("series_key")):
        return
    room = MAX_CHARS - len("\n".join(lines)) - len("\n<i></i>")
    fitted = _fit(label, room)
    if fitted and fitted != ELLIPSIS:
        lines.insert(1, f"<i>{fitted}</i>")


# ── format_exit / format_missed ───────────────────────────────────────────────
def format_exit(event: Mapping[str, Any], now: dt.datetime) -> str:
    """Four lines for a Desk EXIT payload {"advice": str, "position": {...}}:
    EXIT and the instrument, the reason, the estimated P&L (whole rupees, as the Desk
    rounds it) and the time of this message.

    Reads only advice and position.{series_key, kind, side, qty,
    option.{strike, expiry, lot}, unrealized}.
    """
    position = _fields(event.get("position"))
    option = _fields(position.get("option"))
    _, instrument = _instrument(position, option)
    unit = _unit(_plain(position.get("kind")).upper(), position)
    advice = _fit(_plain(event.get("advice")).replace("_", " "), _CAP_REASON) or NA
    return "\n".join(
        [
            f"<b>EXIT {instrument}{SEP}{unit}</b>" if unit else f"<b>EXIT {instrument}</b>",
            f"Reason: {advice}",
            f"Est. P&amp;L {_whole_signed(position.get('unrealized'))}",
            f"Time {_clock(to_ist(now), '%H:%M:%S')}",
        ]
    )


def format_missed(alert: Mapping[str, Any], now: dt.datetime) -> str:
    """One line for an alert whose window passed before it could be delivered. It starts
    with "Missed", never with the action, so a lock-screen preview cannot prompt a trade."""
    deadline = _when(alert.get("expires_at"))
    passed = deadline is None or to_ist(now) >= deadline
    window = f"{'expired' if passed else 'valid till'} {_clock(deadline, '%H:%M')}"
    return SEP.join(["<b>Missed</b>", _header(alert), window, _short_id(alert.get("id"))])


# ── TelegramClient ────────────────────────────────────────────────────────────
API_BASE = "https://api.telegram.org"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"
TIMEOUT = httpx.Timeout(10.0)  # connect, read, write and pool alike
# ASCII only: [0-9], never \d (which also matches full-width and other Unicode digits),
# and always re.fullmatch, never $ (which also matches before a trailing newline).
_TOKEN_SHAPE = re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,}")
_CHAT_SHAPE = re.compile(r"-?[1-9][0-9]{0,18}")
_CAP_DESCRIPTION = 200
_MASK = "***"
# httpx logs every request URL, and so /bot<token>/, at INFO; httpcore logs more at DEBUG.
_HTTPX_LOGGER = "httpx"
_HTTPCORE_LOGGER = "httpcore"
_logging_lock = threading.Lock()  # two clients built at once attach the filter once

SendKind = Literal["ok", "rate_limited", "permanent", "transient", "unavailable"]


@dataclass(frozen=True)
class SendResult:
    """The outcome of one sendMessage call. `retry_after` (seconds) is set only for
    rate_limited; `detail` is built from the HTTP status and Telegram's description
    only, never from exception text (which can quote the URL, and so the token)."""

    ok: bool
    kind: SendKind
    message_id: int | None
    retry_after: float | None
    detail: str


def _failed(kind: SendKind, detail: str, retry_after: float | None = None) -> SendResult:
    return SendResult(False, kind, None, retry_after, detail)


def _token_problem(token: str) -> str | None:
    """Why `token` cannot be used, in words that never repeat any part of it."""
    if not token:
        return f"{TOKEN_ENV} is not set"
    if not _TOKEN_SHAPE.fullmatch(token):
        return (
            f"{TOKEN_ENV} is not a bot token (expected 6 to 12 digits, a colon, then 30 or"
            " more letters, digits, '_' or '-')"
        )
    return None


def _chat_problem(chat_id: str) -> str | None:
    if not chat_id:
        return f"{CHAT_ENV} is not set"
    if not _CHAT_SHAPE.fullmatch(chat_id):
        return (
            f"{CHAT_ENV} is not a numeric chat id (expected an optional '-' then 1 to 19"
            " digits, no leading zero)"
        )
    return None


def _harden_logging(token: str) -> tuple[str, ...]:
    """Make sure no log line can carry `token`, valid or not (a typo in .env is still
    mostly the secret), and return its spellings, longest first.

    * every spelling (the token, its %3A form, the part after the colon) is registered with
      the process-wide redactor by the same rule Secrets uses (telegram_redaction_literals):
      a chat id pasted into the wrong field is never registered, so numbers in logs survive;
    * that redactor is attached to the httpx logger itself, once: a filter on the logger
      scrubs each record before ANY handler sees it, even one added without setup_logging;
    * httpcore (and any child set lower) is raised to WARNING; a stricter level is kept.
    """
    forms = telegram_redaction_literals(token)
    register_secrets(forms)
    redactor = get_redactor()
    with _logging_lock:
        httpx_logger = logging.getLogger(_HTTPX_LOGGER)
        if redactor not in httpx_logger.filters:
            httpx_logger.addFilter(redactor)
        httpcore = logging.getLogger(_HTTPCORE_LOGGER)
        if httpcore.level < logging.WARNING:  # NOTSET too: never inherit a DEBUG root
            httpcore.setLevel(logging.WARNING)
        for name, child in list(logging.Logger.manager.loggerDict.items()):
            if (
                name.startswith(f"{_HTTPCORE_LOGGER}.")
                and isinstance(child, logging.Logger)
                and logging.NOTSET < child.level < logging.WARNING
            ):
                child.setLevel(logging.WARNING)
    return tuple(sorted(forms, key=len, reverse=True))


def _scrub(text: str, forms: tuple[str, ...]) -> str:
    """`text` with every spelling of the token masked (longest first)."""
    for form in forms:
        text = text.replace(form, _MASK)
    return text


def _json_object(response: httpx.Response) -> Mapping[str, Any] | None:
    """The body as a JSON object, or None for anything else (never raises)."""
    try:
        body = response.json()
    except (ValueError, RecursionError):  # not JSON, bad UTF-8, or absurdly deep nesting
        return None
    return body if isinstance(body, dict) else None


def _status_detail(
    status: int, body: Mapping[str, Any] | None, forms: tuple[str, ...], fallback: str = ""
) -> str:
    """'HTTP 403: Forbidden: bot was blocked by the user' — status and description only.
    A description that echoes the request path (a proxy, a bad gateway) has the token
    masked BEFORE the cap, so a cut can never leave part of the secret behind."""
    raw = body.get("description") if body is not None else None
    description = _scrub(_plain(raw), forms)[:_CAP_DESCRIPTION] if isinstance(raw, str) else ""
    note = description or fallback
    return f"HTTP {status}: {note}" if note else f"HTTP {status}"


def _retry_after(body: Mapping[str, Any] | None) -> float | None:
    """parameters.retry_after in seconds; None when absent, not a number or absurd."""
    parameters = body.get("parameters") if body is not None else None
    raw = parameters.get("retry_after") if isinstance(parameters, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        seconds = float(raw)
    except OverflowError:  # a JSON integer too large for a float
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _classify(response: httpx.Response, forms: tuple[str, ...]) -> SendResult:
    """Map one Bot API response to a SendResult (AC3); `forms` are masked in the detail.

    429 is rate_limited; any other 4xx is permanent (the same request fails the same way
    again), except 408, which a proxy sends when it timed out; 2xx needs ok true and an
    integer message_id; everything else (5xx, 3xx, 408) is transient.
    """
    status = response.status_code
    body = _json_object(response)
    if status == 429:
        return _failed("rate_limited", _status_detail(status, body, forms), _retry_after(body))
    if 400 <= status < 500 and status != 408:
        return _failed("permanent", _status_detail(status, body, forms))
    if not 200 <= status < 300:
        return _failed("transient", _status_detail(status, body, forms))
    if body is None:
        return _failed(
            "transient", _status_detail(status, None, forms, "the body is not a JSON object")
        )
    if body.get("ok") is not True:
        return _failed("transient", _status_detail(status, body, forms, "ok is not true"))
    result = body.get("result")
    message_id = result.get("message_id") if isinstance(result, Mapping) else None
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return _failed(
            "transient", _status_detail(status, None, forms, "no message_id in the result")
        )
    return SendResult(True, "ok", message_id, None, f"HTTP {status}")


class TelegramClient:
    """Sends one message per call over the Telegram Bot API (ADR-002, US-003).

    Every outcome comes back as a SendResult, never as an exception. The client never
    retries and never sleeps (the outbox owns back-off and pacing), and it keeps no
    per-call state, so one instance may serve many threads (httpx.Client is thread-safe).
    A missing or malformed token or chat id makes it unavailable: it then makes no HTTP
    call at all, and `unavailable_reason` says which variable is wrong without echoing it.

    The token (AC4) lives only in a private attribute; repr and str are masked; building a
    client hardens logging for it (see `_harden_logging`), and SendResult.detail is built
    from the status and Telegram's description with the token masked, never from an
    exception's text.
    """

    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None) -> None:
        self._forms = _harden_logging(token)  # first, and for a malformed value too
        problems = [p for p in (_token_problem(token), _chat_problem(chat_id)) if p]
        self._unavailable_reason = "; ".join(problems) or None
        self._available = not problems
        # Only a valid pair is kept, so nothing below can ever echo a malformed value.
        self._token = token if self._available else ""
        self._chat_id = chat_id if self._available else ""
        self._owns_http = http is None and self._available
        self._http = httpx.Client(timeout=TIMEOUT) if self._owns_http else http

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] = os.environ, http: httpx.Client | None = None
    ) -> TelegramClient:
        """A client from TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Only CR and LF are
        stripped from the ends (a value saved with a Windows line ending); anything else,
        a space included, must already be right, so a typo is reported, not repaired."""
        token = env.get(TOKEN_ENV, "").strip("\r\n")
        chat_id = env.get(CHAT_ENV, "").strip("\r\n")
        return cls(token, chat_id, http=http)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def send_message(self, text: str, *, silent: bool = False) -> SendResult:
        """POST sendMessage with HTML parse mode; `silent` sends it without a sound.
        Exactly one HTTP request (none when unavailable or closed), with a 10 s timeout on
        every phase whatever the injected client's own default is. Never raises."""
        if not self._available or self._http is None:
            return _failed("unavailable", self._unavailable_reason or "not configured")
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        if silent:
            payload["disable_notification"] = True
        try:
            response = self._http.post(
                f"{API_BASE}/bot{self._token}/sendMessage", json=payload, timeout=TIMEOUT
            )
            return _classify(response, self._forms)
        except httpx.HTTPStatusError as exc:  # an http client that raises for status
            return _classify(exc.response, self._forms)
        except httpx.HTTPError as exc:  # network, timeout, protocol: the class name only
            return _failed("transient", f"network error: {type(exc).__name__}")
        except UnicodeEncodeError:  # a lone surrogate: this text can never be sent
            return _failed("permanent", "the text is not valid UTF-8")
        except Exception as exc:  # noqa: BLE001 - the contract: a SendResult, never a raise
            # The class name only: an exception's text can quote the URL, and so the token.
            log.warning("telegram send failed unexpectedly: %s", type(exc).__name__)
            return _failed("transient", f"unexpected error: {type(exc).__name__}")

    def __repr__(self) -> str:
        return f"TelegramClient(available={self._available}, token={_MASK})"

    __str__ = __repr__

    def close(self) -> None:
        """Stop sending: from now on the client is unavailable and send_message returns
        'unavailable' without a request. The http client this object built is closed; an
        injected one is the caller's to close."""
        if self._available:
            self._available = False
            self._unavailable_reason = "the Telegram client is closed"
        if self._owns_http and self._http is not None:
            self._http.close()
