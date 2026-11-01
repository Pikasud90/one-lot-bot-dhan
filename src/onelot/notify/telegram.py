"""Telegram phone messages for Desk alerts (ADR-002, US-003).

The formatters turn a Desk payload into Telegram HTML that the owner can act on from
the lock screen. The first line says exactly what to do ("BUY BANKNIFTY 45000 CE ·
1 lot (30)"); the rest follows the AC1 order:

    <b>{BUY|SELL} {symbol}[ {strike} {CE|PE}] · {1 share | 1 lot (N)}</b>
    <i>{label}</i>                          omitted when it repeats the symbol
    Entry {price} {mkt | ask | est.}        stock: mkt; index: ask (live chain) | est. (synthetic)
    Underlying Stop {x} · Target {y|trail}  index only
    Stop {x} · Target {y|trail}             stock: share prices; index: premium levels
    {strategy label} {timeframe}
    Conf {NN} {label}
    Valid till {HH:MM} IST ({M}m left | expired)
    Data {live | <b>delayed ~15m</b>}       data.delayed only (US-028 single rule)
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
_CAP_STRATEGY = 48
_CAP_TIMEFRAME = 24
_CAP_CONF_LABEL = 24
_CAP_REASON = 48
_MAX_ABS_NUMBER = Decimal("1e12")  # no price, level or P&L here is a trillion rupees
_CENT = Decimal("0.01")
_UNIT = Decimal(1)

_OPTION_SIDES = ("BUY_CE", "BUY_PE")
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
    """52000.0 -> 52000, 22.5 -> 22.5."""
    q = _rounded(value, _CENT)
    if q is None:
        return _fit(value, _CAP_STRIKE) or "?"
    return f"{q.normalize():f}"


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


def _short_id(value: Any) -> str:
    return f"id <code>{_esc(_plain(value)[:ID_CHARS])}</code>"


# ── shared pieces ────────────────────────────────────────────────────────────
def _instrument(payload: Mapping[str, Any], option: Mapping[str, Any]) -> tuple[str, str]:
    """(action word, instrument) for an alert or a position, both escaped. The CE/PE
    comes from the Desk side ("BUY_CE"), never from free text."""
    side = _plain(payload.get("side")).upper()
    symbol = _fit(payload.get("series_key"), _CAP_SYMBOL) or "?"
    if side in _OPTION_SIDES:
        return "BUY", f"{symbol} {_strike(option.get('strike'))} {side[-2:]}"
    if side in ("BUY", "SELL"):
        return side, symbol
    return (_fit(side.replace("_", " "), _CAP_STRIKE) or "?"), symbol


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
    """'BUY BANKNIFTY 45000 CE · 1 lot (30)' — escaped, without tags."""
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

    Reads only: id, created_at, expires_at, series_key, label, kind, side,
    strategy_label (else strategy), timeframe, confidence.{score,label}, data.delayed and
    economics.{qty, entry_price, stop_price, target_price, underlying_stop,
    underlying_target, synthetic, option.{strike,lot}}. `now` drives the minutes left,
    so format at send time.
    """
    economics = _fields(alert.get("economics"))
    index = _plain(alert.get("kind")).upper() == "INDEX"
    if not index:
        entry_tag = "mkt"
    else:  # a live-chain premium is the ask you pay; anything else is a model estimate
        entry_tag = "ask" if economics.get("synthetic") is False else "est."
    lines = [
        f"<b>{_header(alert)}</b>",
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
    score = _rounded(confidence.get("score"), _UNIT)
    shown = NA if score is None else f"{score:f}"
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
    # US-028: data.delayed is the single rule; data.source is never re-derived here.
    return f"Data <b>{DELAYED_TAG}</b>" if data.get("delayed") is True else "Data live"


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

    Reads only advice and position.{series_key, kind, side, qty, option.{strike,lot},
    unrealized}.
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
