"""Telegram phone messages for Desk alerts (ADR-002, US-003).

The formatters turn a Desk payload into Telegram HTML that the owner can act on from
the lock screen: the first line says exactly what to do ("BUY BANKNIFTY 45000 CE ·
1 lot (30)"), the rest gives levels, strategy, confidence, the deadline and the feed.

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
import re
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..clock import to_ist

MAX_LINES = 12
MAX_CHARS = 600
ELLIPSIS = "…"
SEP = " · "
NA = "n/a"
DELAYED_TAG = "delayed ~15m"
ID_CHARS = 6

# Caps in HTML characters after escaping; with these the fixed part of the longest
# message (an index alert) stays well under MAX_CHARS whatever the input.
_CAP_SYMBOL = 32
_CAP_STRIKE = 12
_CAP_UNIT = 20
_CAP_STRATEGY = 32
_CAP_TIMEFRAME = 8
_CAP_CONF_LABEL = 24
_CAP_REASON = 32
_MAX_ABS_NUMBER = Decimal("1e12")  # no price, level or P&L here is a trillion rupees
_CENT = Decimal("0.01")

_OPTION_SIDES = ("BUY_CE", "BUY_PE")
_EXIT_REASONS = {
    "STOP": "stop hit",
    "TRAIL_STOP": "trailing stop hit",
    "TARGET": "target hit",
    "SIGNAL_EXIT": "strategy exit signal",
    "OPPOSITE_SIGNAL": "opposite signal",
    "SQUARE_OFF": "square-off time",
    "TIME_STOP": "time stop",
}
# C0/C1 controls that are not whitespace, and bidi overrides that could re-order a line.
_INVISIBLE = re.compile(
    "[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]"
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


def _num(value: Any, prefix: str = "") -> str:
    """2-decimal, comma-grouped, rounded half-up (2.675 -> 2.68, unlike float formatting)."""
    number = _decimal(value)
    if number is None:
        return NA
    q = number.quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"{'-' if q < 0 else ''}{prefix}{abs(q):,.2f}"


def _signed(value: Any, prefix: str = "", suffix: str = "") -> str:
    number = _decimal(value)
    if number is None:
        return NA
    q = number.quantize(_CENT, rounding=ROUND_HALF_UP)
    sign = "+" if q > 0 else ("-" if q < 0 else "")
    return f"{sign}{prefix}{abs(q):,.2f}{suffix}"


def _strike(value: Any) -> str:
    number = _decimal(value)
    if number is None:
        return _fit(value, _CAP_STRIKE) or "?"
    return f"{number.quantize(_CENT, rounding=ROUND_HALF_UP).normalize():f}"


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


def _hms(moment: dt.datetime | None) -> str:
    return f"{moment:%H:%M:%S} IST" if moment is not None else NA


def _short_id(value: Any) -> str:
    return "#" + _esc(_plain(value)[:ID_CHARS])


# ── shared pieces ────────────────────────────────────────────────────────────
def _contract(payload: Mapping[str, Any], option: Mapping[str, Any]) -> tuple[str, str]:
    """(action word, instrument) for an alert or a position, both escaped.
    Options take the CE/PE from the Desk side ("BUY_CE"), never from free text."""
    side = _plain(payload.get("side")).upper()
    symbol = _fit(payload.get("series_key"), _CAP_SYMBOL) or "?"
    if side in _OPTION_SIDES:
        return "BUY", f"{symbol} {_strike(option.get('strike'))} {side[-2:]}"
    if side in ("BUY", "SELL"):
        return side, symbol
    return (_fit(side.replace("_", " "), _CAP_STRIKE) or "?"), symbol


def _header(alert: Mapping[str, Any]) -> str:
    """'BUY BANKNIFTY 45000 CE · 1 lot (30)' — escaped, without tags."""
    economics = _fields(alert.get("economics"))
    action, instrument = _contract(alert, _fields(economics.get("option")))
    unit = _fit(economics.get("unit_label"), _CAP_UNIT)
    return f"{action} {instrument}{SEP}{unit}" if unit else f"{action} {instrument}"


def _level_pair(stop: str, target: Any, money: str, target_word: str, trail: str) -> str:
    tail = f"{target_word} {_num(target, money)}" if target is not None else trail
    return f"{stop}{SEP}{tail}"


# ── public formatters ─────────────────────────────────────────────────────────
def format_alert(alert: Mapping[str, Any], now: dt.datetime) -> str:
    """The phone message for a Desk SIGNAL dict (see `Desk._raise_signal`).

    Reads only: id, created_at, expires_at, series_key, label, kind, side, strategy,
    timeframe, confidence.{score,label}, data.delayed and economics.{unit_label,
    entry_price, stop_price, target_price, underlying_stop, underlying_target,
    synthetic, option.strike}. `now` drives the minutes left (formatted at send time).
    """
    economics = _fields(alert.get("economics"))
    index = _plain(alert.get("kind")).upper() == "INDEX"
    if not index:
        entry_tag = "mkt"
    else:  # a live-chain premium is the ask you pay; anything else is a model estimate
        entry_tag = "mkt (ask)" if economics.get("synthetic") is False else "est."
    lines = [
        f"<b>{_header(alert)}</b>",
        f"Entry {_num(economics.get('entry_price'), '₹')} {entry_tag}",
        _level_pair(
            f"Stop {_num(economics.get('stop_price'), '₹')}",
            economics.get("target_price"),
            "₹",
            "Target",
            "trail (no target)",
        ),
    ]
    if index:
        lines.append(
            _level_pair(
                f"Underlying stop {_num(economics.get('underlying_stop'))}",
                economics.get("underlying_target"),
                "",
                "target",
                "trail",
            )
        )
    lines += [
        _strategy_line(alert),
        _ttl_line(_when(alert.get("expires_at")), to_ist(now)),
        _data_line(alert),
    ]
    _insert_label(lines, alert)
    return "\n".join(lines)


def _strategy_line(alert: Mapping[str, Any]) -> str:
    name = " ".join(
        part
        for part in (
            _fit(alert.get("strategy"), _CAP_STRATEGY),
            _fit(alert.get("timeframe"), _CAP_TIMEFRAME),
        )
        if part
    )
    conf = _fields(alert.get("confidence"))
    score = _decimal(conf.get("score"))
    shown = NA if score is None else f"{score.quantize(Decimal(1), rounding=ROUND_HALF_UP):f}"
    label = _fit(conf.get("label"), _CAP_CONF_LABEL)
    confidence = f"conf {shown} {label}" if label else f"conf {shown}"
    return f"{name}{SEP}{confidence}" if name else confidence


def _ttl_line(deadline: dt.datetime | None, now: dt.datetime) -> str:
    if deadline is None:
        return f"Valid till {NA}"
    minutes = int((deadline - now).total_seconds() // 60)
    left = f"{minutes} min left" if minutes > 0 else "expired"
    return f"Valid till {_hms(deadline)}{SEP}{left}"


def _data_line(alert: Mapping[str, Any]) -> str:
    # US-028: data.delayed is the single rule; data.source is never re-derived here.
    delayed = _fields(alert.get("data")).get("delayed") is True
    feed = f"<b>{DELAYED_TAG}</b>" if delayed else "live"
    created = _hms(_when(alert.get("created_at")))
    return f"Data {feed}{SEP}alert {created}{SEP}{_short_id(alert.get('id'))}"


def _insert_label(lines: list[str], alert: Mapping[str, Any]) -> None:
    """Put the instrument label under the header, in the room the budget leaves."""
    label = _plain(alert.get("label"))
    if not label or label == _plain(alert.get("series_key")):
        return
    room = MAX_CHARS - len("\n".join(lines)) - len("\n<i></i>")
    fitted = _fit(label, room)
    if fitted and fitted != ELLIPSIS:
        lines.insert(1, f"<i>{fitted}</i>")


def format_exit(event: Mapping[str, Any], now: dt.datetime) -> str:
    """At most four lines for a Desk EXIT payload {"advice": str, "position": {...}}.

    Reads only advice and position.{id, series_key, kind, side, qty, option.strike,
    unrealized, r_now}.
    """
    advice = _plain(event.get("advice")).upper()
    position = _fields(event.get("position"))
    _, instrument = _contract(position, _fields(position.get("option")))
    kind = _plain(position.get("kind")).upper()
    unit = ""
    if kind == "INDEX":
        unit = f"{SEP}1 lot ({_fit(position.get('qty'), _CAP_STRIKE) or '?'})"
    elif kind == "STOCK":
        short = _plain(position.get("side")).upper() == "SELL"
        unit = f"{SEP}1 share" + (" (short)" if short else "")
    reason = _EXIT_REASONS.get(advice) or (
        _fit(advice.replace("_", " ").lower(), _CAP_REASON) or "exit advised"
    )
    pnl = _signed(position.get("unrealized"), "₹")
    r_now = _signed(position.get("r_now"), suffix="R")
    pnl_line = f"Est. P&amp;L {pnl}" + (f" ({r_now})" if pnl != NA and r_now != NA else "")
    return "\n".join(
        [
            f"<b>EXIT {instrument}{unit}</b>",
            f"Reason: {reason}",
            pnl_line,
            f"{_hms(to_ist(now))}{SEP}{_short_id(position.get('id'))}",
        ]
    )


def format_missed(alert: Mapping[str, Any], now: dt.datetime) -> str:
    """One line for an alert whose window passed before it could be delivered. It starts
    with "Missed", never with the action, so a lock-screen preview cannot prompt a trade."""
    deadline = _when(alert.get("expires_at"))
    if deadline is not None and to_ist(now) < deadline:
        window = f"valid till {_hms(deadline)}"
    else:
        window = f"expired {_hms(deadline)}"
    return SEP.join(["<b>Missed</b>", _header(alert), window, _short_id(alert.get("id"))])
