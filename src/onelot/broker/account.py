"""Read-only account service over Dhan's FREE trading API.

Everything here is a GET: profile, funds, holdings, positions, ledger and the
trade book. None of it needs the paid Data API, so it works on day one with a
free-tier token. Nothing here can place, modify or cancel anything.

Caching policy (the Home tab must never poll the broker): every snapshot is
stored in `account_cache` and served from there until something that could
change it happens -- the operator presses Refresh, or confirms a fill / an
exit in the engine. A snapshot older than `stale_after` is refreshed lazily
on the next read, at most once.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any

from sqlalchemy import Engine, select

from ..clock import IST, now_ist
from ..data.db import upsert
from ..data.schema import account_cache
from .base import BrokerError, DataEntitlementError, TokenExpiredError

log = logging.getLogger(__name__)

KEYS = ("profile", "funds", "holdings", "positions", "ledger", "trades")


def _f(v: Any) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _voucher_date(v: Any) -> dt.date | None:
    """Dhan ledger dates look like 'Oct 27, 2026'; tolerate ISO too."""
    s = str(v or "").strip()
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _split_error(raw: str | None) -> tuple[str | None, str | None]:
    """'<iso>|<message>' -> (message, iso); tolerate the old bare format."""
    if not raw:
        return None, None
    head, sep, tail = raw.partition("|")
    if sep and len(head) >= 19 and head[4] == "-" and "T" in head:
        return tail, head
    return raw, None


class AccountService:
    def __init__(
        self,
        engine: Engine,
        adapter=None,
        stale_after: dt.timedelta = dt.timedelta(hours=12),
        ledger_days: int = 30,
        trades_days: int = 30,
    ) -> None:
        self.engine = engine
        self.adapter = adapter
        self.stale_after = stale_after
        self.ledger_days = ledger_days
        self.trades_days = trades_days
        self._lock = threading.Lock()

    # ── storage ───────────────────────────────────────────────────────────
    def _read(self, key: str) -> dict | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(account_cache).where(account_cache.c.key == key))
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def _write(self, key: str, payload: Any, error: str | None) -> None:
        """A failed refresh must never blank the panel: keep the last good
        payload and its fetch time, and record the error with its own time.
        (The token expires daily and the laptop is not always online; the
        operator still needs to see yesterday's ledger, clearly marked.)"""
        now = now_ist()
        if error is None:
            upsert(
                self.engine,
                account_cache,
                [{"key": key, "fetched_at": now, "payload": payload, "error": None}],
                ["key"],
            )
            return
        prev = self._read(key) or {}
        upsert(
            self.engine,
            account_cache,
            [
                {
                    "key": key,
                    "fetched_at": prev.get("fetched_at")
                    if prev.get("payload") is not None
                    else None,
                    "payload": prev.get("payload"),
                    "error": f"{now.isoformat()}|{error}",
                }
            ],
            ["key"],
        )

    # ── fetch ─────────────────────────────────────────────────────────────
    def _fetch(self, key: str) -> Any:
        a = self.adapter
        if a is None:
            raise BrokerError("no broker credentials configured")
        today = now_ist().date()
        if key == "profile":
            return a.profile()
        if key == "funds":
            return a.fund_limits()
        if key == "holdings":
            return a.holdings()
        if key == "positions":
            return a.positions()
        if key == "ledger":
            return a.ledger(today - dt.timedelta(days=self.ledger_days), today)
        if key == "trades":
            return a.trade_history(today - dt.timedelta(days=self.trades_days), today)
        raise KeyError(key)

    def refresh(self, keys: tuple[str, ...] | list[str] = KEYS) -> dict[str, dict]:
        """Hit the broker for `keys` and update the cache. Errors are cached too,
        so the UI can show *why* a panel is empty without re-hitting the API."""
        out: dict[str, dict] = {}
        with self._lock:
            for k in keys:
                try:
                    payload = self._fetch(k)
                    self._write(k, payload, None)
                    out[k] = {"ok": True}
                except TokenExpiredError as exc:
                    self._write(k, None, f"TOKEN_EXPIRED: {exc}")
                    out[k] = {"ok": False, "error": "TOKEN_EXPIRED"}
                except DataEntitlementError as exc:
                    self._write(k, None, f"NO_DATA_API: {exc}")
                    out[k] = {"ok": False, "error": "NO_DATA_API"}
                except Exception as exc:  # noqa: BLE001
                    self._write(k, None, f"{type(exc).__name__}: {str(exc)[:300]}")
                    out[k] = {"ok": False, "error": type(exc).__name__}
                    log.warning("account refresh %s failed: %s", k, exc)
        return out

    def on_trade_event(self) -> None:
        """Called by the engine after a fill or an exit is confirmed: the only
        automatic refresh, because that is the only moment the account changes."""
        if self.adapter is None:
            return
        threading.Thread(
            target=self.refresh,
            args=(("funds", "positions", "trades"),),
            name="account-refresh",
            daemon=True,
        ).start()

    # ── reads (cached) ────────────────────────────────────────────────────
    def snapshot(self, refresh: bool = False) -> dict:
        if refresh and self.adapter is not None:
            self.refresh()
        out: dict[str, Any] = {"configured": self.adapter is not None, "panels": {}}
        oldest: dt.datetime | None = None
        last_err: tuple[str, str, str] | None = None  # (error_at, key, message)
        for k in KEYS:
            row = self._read(k)
            if row is None:
                out["panels"][k] = {
                    "data": None,
                    "fetched_at": None,
                    "error": None,
                    "error_at": None,
                }
                continue
            fa = row.get("fetched_at")
            if fa is not None and fa.tzinfo is None:
                fa = fa.replace(tzinfo=dt.timezone.utc)
            if fa is not None:
                fa = fa.astimezone(IST)
                oldest = fa if oldest is None or fa < oldest else oldest
            err, err_at = _split_error(row.get("error"))
            if err and (last_err is None or (err_at or "") > last_err[0]):
                last_err = (err_at or "", k, err)
            out["panels"][k] = {
                "data": row.get("payload"),
                "fetched_at": fa.isoformat() if fa else None,
                "error": err,
                "error_at": err_at,
            }
        out["fetched_at"] = oldest.isoformat() if oldest else None
        out["stale"] = bool(oldest and (now_ist() - oldest) > self.stale_after)
        out["last_error"] = (
            {"at": last_err[0] or None, "panel": last_err[1], "message": last_err[2]}
            if last_err
            else None
        )
        out["token_expired"] = any(
            (p.get("error") or "").startswith("TOKEN_EXPIRED") for p in out["panels"].values()
        )
        out["offline"] = any(
            "ConnectError" in (p.get("error") or "") or "network error" in (p.get("error") or "")
            for p in out["panels"].values()
        )
        out["summary"] = self._summary(out["panels"])
        return out

    @staticmethod
    def _summary(panels: dict) -> dict:
        funds = (panels.get("funds") or {}).get("data") or {}
        holdings = (panels.get("holdings") or {}).get("data") or []
        positions = (panels.get("positions") or {}).get("data") or []
        profile = (panels.get("profile") or {}).get("data") or {}
        ledger = (panels.get("ledger") or {}).get("data") or []
        open_pos = [p for p in positions if isinstance(p, dict) and int(_f(p.get("netQty"))) != 0]
        realised = sum(_f(p.get("realizedProfit")) for p in positions if isinstance(p, dict))
        unreal = sum(_f(p.get("unrealizedProfit")) for p in positions if isinstance(p, dict))
        hold_rows = (
            [h for h in holdings if isinstance(h, dict)] if isinstance(holdings, list) else []
        )
        invested = sum(_f(h.get("avgCostPrice")) * _f(h.get("totalQty")) for h in hold_rows)
        # Dhan's holdings carry lastTradedPrice (as of the last fetch): mark the
        # book without touching the paid Data API.
        priced = [h for h in hold_rows if _f(h.get("lastTradedPrice")) > 0]
        value = (
            sum(_f(h.get("lastTradedPrice")) * _f(h.get("totalQty")) for h in priced)
            if priced
            else None
        )
        priced_cost = (
            sum(_f(h.get("avgCostPrice")) * _f(h.get("totalQty")) for h in priced)
            if priced
            else None
        )
        holdings_pnl = (value - priced_cost) if value is not None else None
        # Dhan returns the ledger NEWEST first; never trust the list order --
        # take the row with the latest voucher date (ties: first seen).
        last_bal = None
        last_date = None
        debits = credits = 0.0
        if isinstance(ledger, list):
            rows = [r for r in ledger if isinstance(r, dict)]
            for r in rows:
                debits += _f(r.get("debit"))
                credits += _f(r.get("credit"))
            dated = [(_voucher_date(r.get("voucherdate")), i, r) for i, r in enumerate(rows)]
            dated = [x for x in dated if x[0] is not None]
            if dated:
                d, _i, r = max(dated, key=lambda x: (x[0], -x[1]))
                last_bal, last_date = _f(r.get("runbal")), d.isoformat()
            elif rows:
                last_bal = _f(rows[0].get("runbal"))
        return {
            "client_id_suffix": (str(profile.get("dhanClientId") or "")[-4:] or None),
            "token_validity": profile.get("tokenValidity"),
            "data_plan": profile.get("dataPlan"),
            "data_validity": profile.get("dataValidity"),
            "active_segments": profile.get("activeSegment"),
            "available_balance": _f(funds.get("availabelBalance") or funds.get("availableBalance")),
            "withdrawable_balance": _f(funds.get("withdrawableBalance")),
            "utilized_amount": _f(funds.get("utilizedAmount")),
            "collateral": _f(funds.get("collateralAmount")),
            "holdings_count": len(hold_rows),
            "holdings_invested": invested,
            "holdings_value": value,
            "holdings_pnl": holdings_pnl,
            "open_positions": len(open_pos),
            "positions_realized": realised,
            "positions_unrealized": unreal,
            "ledger_running_balance": last_bal,
            "ledger_last_date": last_date,
            "ledger_debits": debits,
            "ledger_credits": credits,
        }
