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


class AccountService:
    def __init__(self, engine: Engine, adapter=None, stale_after: dt.timedelta = dt.timedelta(hours=12),
                 ledger_days: int = 30, trades_days: int = 30) -> None:
        self.engine = engine
        self.adapter = adapter
        self.stale_after = stale_after
        self.ledger_days = ledger_days
        self.trades_days = trades_days
        self._lock = threading.Lock()

    # ── storage ───────────────────────────────────────────────────────────
    def _read(self, key: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(account_cache).where(account_cache.c.key == key)).mappings().first()
        return dict(row) if row else None

    def _write(self, key: str, payload: Any, error: str | None) -> None:
        upsert(self.engine, account_cache,
               [{"key": key, "fetched_at": now_ist(), "payload": payload, "error": error}], ["key"])

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
        threading.Thread(target=self.refresh, args=(("funds", "positions", "trades"),),
                         name="account-refresh", daemon=True).start()

    # ── reads (cached) ────────────────────────────────────────────────────
    def snapshot(self, refresh: bool = False) -> dict:
        if refresh and self.adapter is not None:
            self.refresh()
        out: dict[str, Any] = {"configured": self.adapter is not None, "panels": {}}
        oldest: dt.datetime | None = None
        for k in KEYS:
            row = self._read(k)
            if row is None:
                out["panels"][k] = {"data": None, "fetched_at": None, "error": None}
                continue
            fa = row.get("fetched_at")
            if fa is not None and fa.tzinfo is None:
                fa = fa.replace(tzinfo=dt.timezone.utc)
            if fa is not None:
                fa = fa.astimezone(IST)
                oldest = fa if oldest is None or fa < oldest else oldest
            out["panels"][k] = {"data": row.get("payload"), "fetched_at": fa.isoformat() if fa else None,
                                "error": row.get("error")}
        out["fetched_at"] = oldest.isoformat() if oldest else None
        out["stale"] = bool(oldest and (now_ist() - oldest) > self.stale_after)
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
        invested = sum(_f(h.get("avgCostPrice")) * _f(h.get("totalQty")) for h in holdings if isinstance(h, dict))
        last_bal = None
        if ledger and isinstance(ledger, list) and isinstance(ledger[-1], dict):
            last_bal = _f(ledger[-1].get("runbal"))
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
            "holdings_count": len(holdings) if isinstance(holdings, list) else 0,
            "holdings_invested": invested,
            "open_positions": len(open_pos),
            "positions_realized": realised,
            "positions_unrealized": unreal,
            "ledger_running_balance": last_bal,
        }
