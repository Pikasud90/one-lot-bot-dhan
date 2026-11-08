"""The paper book as the desk shows it (US-030): JSON-ready dicts. Pure: no I/O.

Money leaves the ledger as integer paise and becomes a rupee string only here, through
`format_rupees` (Decimal, exact, never a float). Times become ISO-8601 in IST. Whatever is
not known yet (an entry before the fill, a net before costs exist) is None, i.e. JSON null:
nothing is ever invented for display.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Any, TypedDict

from ..clock import IST
from .ledger import Slot  # at run time: the ledger imports neither the book nor this module
from .money import format_rupees, to_decimal

if TYPE_CHECKING:  # annotation only: book.py imports this module
    from .book import PaperTrade
    from .ledger import StrategyTotals

TERMINAL_STATUSES = ("NO_FILL", "CLOSED")  # final: the trade is no longer managed


class DayTotals(TypedDict):
    """PaperBook.today_totals(): the CLOSED trades closed on one IST date, in paise."""

    closed: int
    wins: int
    losses: int
    gross_paise: int | None  # None if any of them has no gross
    net_paise: int | None  # None if any of them has no net (costs are US-023)


def trade_view(trade: PaperTrade) -> dict[str, Any]:
    """One paper trade for GET /api/paper: entry/exit/mark are per unit, gross/net/unrealised
    the whole trade. `mark` and `unrealised` are set for an OPEN trade only (`open_mark`)."""
    plan = trade.plan  # parsed once for the label and the mark
    mark, unrealised = open_mark(trade, plan)
    return {
        "alert_id": trade.alert_id,
        "instrument": instrument_label(plan, trade.kind, trade.series_key),
        "side": trade.side,
        "direction": trade.direction,
        "kind": trade.kind,
        "strategy": trade.strategy,
        "timeframe": trade.timeframe,
        "grade": trade.grade,
        "status": trade.status,
        "close_reason": trade.close_reason,
        "qty": trade.qty,
        "entry_ts": _iso(trade.entry_ts),
        "entry": format_rupees(trade.entry_paise),
        "exit": format_rupees(trade.exit_paise),
        "gross": format_rupees(trade.gross_paise),
        "net": format_rupees(trade.net_paise),
        "mark": format_rupees(mark),
        "unrealised": format_rupees(unrealised),
        "created_at": _iso(trade.created_at),
        "closed_at": _iso(closed_at(trade)),
    }


def open_mark(
    trade: PaperTrade, plan: Mapping[str, Any] | None = None
) -> tuple[int | None, int | None]:
    """(mark, unrealised) of an OPEN trade in paise, or (None, None) when it has none.

    A stock is marked at the close of the last bar managed, unslipped (the position's
    last_close_paise); an index option at the premium the book stored with that close
    (mark_paise, US-023 AC3), None when repricing failed: never 0, never invented.
    unrealised = sign x (mark - entry) x qty, gross of costs; an option is long premium.
    A waiting, closed or NO_FILL trade has neither. `plan` is `trade.plan` if already parsed.
    """
    if trade.status != "OPEN" or trade.entry_paise is None:
        return None, None
    position = (trade.plan if plan is None else plan).get("position")
    if not isinstance(position, Mapping):
        return None, None
    if trade.kind == "INDEX":
        raw, sign = position.get("mark_paise"), 1
    else:
        raw, sign = position.get("last_close_paise"), 1 if trade.direction == "long" else -1
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, None
    return raw, sign * (raw - trade.entry_paise) * trade.qty


def closed_at(trade: PaperTrade) -> dt.datetime | None:
    """When a final trade was closed: its exit time, else (a NO_FILL has none) the time the
    book wrote the close. None while the trade is still waiting or open."""
    if trade.status not in TERMINAL_STATUSES:
        return None
    return trade.exit_ts if trade.exit_ts is not None else trade.updated_at


def instrument_label(plan: Mapping[str, Any], kind: str, fallback: str) -> str:
    """The alert's label; for an index alert, plus the option it named.

    "RELIANCE"; "BANKNIFTY 45000 CE 2026-11-24". A part the plan lacks is left out, and a
    plan without a label falls back to `fallback` (the series key).
    """
    alert = plan.get("alert") if isinstance(plan, Mapping) else None
    alert = alert if isinstance(alert, Mapping) else {}
    label = str(alert.get("label") or "").strip() or fallback
    economics = alert.get("economics")
    option = economics.get("option") if isinstance(economics, Mapping) else None
    if kind != "INDEX" or not isinstance(option, Mapping):
        return label
    parts = [label]
    try:
        parts.append(format(to_decimal(option.get("strike")).normalize(), "f"))
    except ValueError:
        pass  # no usable strike: say nothing rather than a wrong one
    parts += [str(option.get(key) or "").strip() for key in ("opt_type", "expiry")]
    return " ".join(part for part in parts if part)


def totals_view(totals: DayTotals) -> dict[str, Any]:
    """A day's totals for GET /api/paper: the money as rupee strings (None stays null)."""
    return {
        "closed": totals["closed"],
        "wins": totals["wins"],
        "losses": totals["losses"],
        "gross_rupees": format_rupees(totals["gross_paise"]),
        "net_rupees": format_rupees(totals["net_paise"]),
    }


def last_session_view(day: dt.date, totals: DayTotals) -> dict[str, Any]:
    """GET /api/paper's `last_session` (US-032): {date: "YYYY-MM-DD" (IST), totals}."""
    return {"date": day.isoformat(), "totals": totals_view(totals)}


def scoreboard_view(
    totals: Mapping[str, StrategyTotals],
    primary: Sequence[str] = (),
    open_counts: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """The Home strategy scoreboard (owner feedback OF-5): one row per strategy from the
    paper book, the `primary` strategies first in the owner's order (listed even before
    their first trade), then any other strategy that traded, by net P&L.

    win_rate is a percent of the trades with a net (None before the first one); money is a
    rupee string from exact paise; avg_net is rounded half-even to the paisa; profit_factor
    is won / |lost| (None when nothing was lost); avg_r is the mean R multiple.
    """
    opened = open_counts or {}
    names = list(dict.fromkeys(primary))
    others = sorted(
        (name for name in totals if name not in names),
        key=lambda name: -totals[name].net_paise,
    )
    return [
        {"strategy": name, "primary": name in names, **_score(totals.get(name), opened, name)}
        for name in [*names, *others]
    ]


def slot_scoreboard_view(
    totals: Mapping[Slot, StrategyTotals],
    primary: Sequence[tuple[str, str, str]] = (),
    open_counts: Mapping[Slot, int] | None = None,
) -> list[dict[str, Any]]:
    """The Home scoreboard of the paper test (2026-11-08): `scoreboard_view`'s rows, one per
    slot (strategy, timeframe, market), each row with the slot's `timeframe` and `kind`
    (INDEX | STOCK) beside its `strategy`. The `primary` slots come first in the given order,
    listed even before their first trade; then any other slot that traded, by net P&L. A
    primary strategy's trades on another timeframe or market (an earlier setting) stay in
    their own row, which is not primary, so they never count toward the paper test."""
    opened = open_counts or {}
    slots = list(dict.fromkeys(Slot(*s) for s in primary))
    others = sorted(
        (slot for slot in totals if slot not in slots),
        key=lambda slot: -totals[slot].net_paise,
    )
    return [
        {
            "strategy": slot.strategy,
            "timeframe": slot.timeframe,
            "kind": slot.kind,
            "primary": slot in slots,
            **_score(totals.get(slot), opened, slot),
        }
        for slot in [*slots, *others]
    ]


def _score(row: StrategyTotals | None, opened: Mapping[Any, int], key: object) -> dict[str, Any]:
    """One scoreboard row's numbers (see `scoreboard_view`)."""
    trades = row.trades if row else 0
    avg = (
        None
        if not row or not trades
        else int(
            (Decimal(row.net_paise) / Decimal(trades)).quantize(
                Decimal(1), rounding=ROUND_HALF_EVEN
            )
        )
    )
    return {
        "trades": trades,
        "wins": row.wins if row else 0,
        "losses": row.losses if row else 0,
        "no_fill": row.no_fill if row else 0,
        "open": int(opened.get(key, 0)),
        "win_rate": round(100.0 * row.wins / trades, 1) if row and trades else None,
        "net": format_rupees(row.net_paise) if row and trades else None,
        "costs": format_rupees(row.costs_paise) if row and trades else None,
        "avg_net": format_rupees(avg),
        "profit_factor": (
            round(row.won_paise / -row.lost_paise, 2) if row and row.lost_paise < 0 else None
        ),
        "avg_r": round(row.r_sum / row.r_trades, 2) if row and row.r_trades else None,
        "last_exit": _iso(row.last_exit) if row else None,
    }


def _iso(ts: dt.datetime | None) -> str | None:
    return None if ts is None else ts.astimezone(IST).isoformat()
