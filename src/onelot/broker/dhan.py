"""Dhan broker adapter — READ ONLY.

Design decision worth stating explicitly:

    Request/response endpoints go through DIRECT REST (httpx) against Dhan's
    documented v2 API, not through the `dhanhq` SDK. The SDK's method
    signatures have changed materially between 1.x and 2.x (DhanContext,
    renamed kwargs, reshuffled return envelopes). Pinning ourselves to those
    signatures means a routine `pip install -U` can break a live trading
    session. The HTTP contract is far more stable than the Python wrapper.

    The SDK is used only for the WebSocket market feed, where reimplementing
    the binary protocol would be a genuine liability. If the SDK is missing or
    its feed API has moved, the adapter degrades to REST polling and reports
    DEGRADED rather than dying.

Every call passes through the shared RateLimiter (v1 defect D3) and every
failure mode is typed: RateLimitError, TokenExpiredError, DataEntitlementError.

*** UNVERIFIED-AGAINST-LIVE NOTICE ***
Endpoint paths and payload shapes below follow Dhan's published v2 API. They
have NOT been executed against a live account in this build because no
credentials are present. `python -m onelot.preflight` exercises every one of
them and reports precisely which succeed. Run it before trusting this file.
Response-shape handling is deliberately tolerant of key-name variation.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import threading
from typing import Any

import httpx

from ..clock import IST, to_ist
from ..ratelimit import RateLimiter
from .base import (
    Bar,
    BrokerError,
    ChainRow,
    ChainSnapshot,
    Contract,
    DataEntitlementError,
    QuoteSnapshot,
    RateLimitError,
    TickCallback,
    TokenExpiredError,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

EP_EXPIRY_LIST = "/optionchain/expirylist"
EP_OPTION_CHAIN = "/optionchain"
EP_QUOTE = "/marketfeed/quote"
EP_INTRADAY = "/charts/intraday"
EP_HISTORICAL = "/charts/historical"

# Free Trading-API reads (no Data API subscription needed). All GET.
EP_PROFILE = "/profile"
EP_FUNDS = "/fundlimit"
EP_HOLDINGS = "/holdings"
EP_POSITIONS = "/positions"
EP_LEDGER = "/ledger"
EP_TRADES = "/trades"

# Dhan error codes (annexure). DH-902 / 806 = Data API not subscribed.
_DATA_ENTITLEMENT_CODES = {"DH-902", "806"}
_TOKEN_CODES = {"DH-901", "807", "808", "809", "810"}


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


_DATA_HELP = ("Dhan's live feed, historical bars and option chain need the paid Data API "
              "add-on (Rs 499 + GST per month), separate from the free trading API.")


def _error_fields(body: Any) -> tuple[str, str, str]:
    """(status, code, message) from either Dhan error envelope.

    Dhan answers in two shapes:
      {"errorType": "Invalid_Access", "errorCode": "DH-902", "errorMessage": "..."}
      {"data": {"806": "Data APIs not Subscribed"}, "status": "failed"}
    The second one hides the code as a KEY inside `data`.
    """
    if not isinstance(body, dict):
        return "", "", ""
    status = str(_first(body, "status", default="") or "").lower()
    code = str(_first(body, "errorCode", "error_code", "errorType", default="") or "")
    msg = str(_first(body, "errorMessage", "error_message", "remarks", default="") or "")
    inner = body.get("data")
    if not code and status in ("failed", "failure") and isinstance(inner, dict) and inner:
        k, v = next(iter(inner.items()))
        code, msg = str(k), str(v)
    return status, code, msg


def _is_entitlement(code: str, msg: str) -> bool:
    m = msg.lower()
    return code in _DATA_ENTITLEMENT_CODES or "not subscribed" in m or "data api" in m


# Dhan's exchange + segment-code columns -> the exchangeSegment enum its APIs speak.
_SEGMENT_ENUM = {("NSE", "I"): "IDX_I", ("BSE", "I"): "IDX_I", ("NSE", "E"): "NSE_EQ", ("BSE", "E"): "BSE_EQ",
                 ("NSE", "D"): "NSE_FNO", ("BSE", "D"): "BSE_FNO", ("NSE", "C"): "NSE_CURRENCY",
                 ("BSE", "C"): "BSE_CURRENCY", ("MCX", "M"): "MCX_COMM"}


def scrip_fields(r: dict) -> dict:
    """Normalise one scrip-master row from EITHER csv layout.

    api-scrip-master.csv          SEM_EXM_EXCH_ID, SEM_SEGMENT, SEM_INSTRUMENT_NAME (FUTIDX), SEM_TRADING_SYMBOL ...
    api-scrip-master-detailed.csv EXCH_ID, SEGMENT, INSTRUMENT (FUTIDX), UNDERLYING_SYMBOL, SYMBOL_NAME, LOT_SIZE ...

    The detailed file's INSTRUMENT_TYPE column holds a different thing ("FUT" / "OP" / "INDEX");
    reading it as the instrument type made every future and option invisible, so the engine
    resolved lot size 0 and no futures contract, and typed the index as NSE_FNO instead of IDX_I.
    """
    exch = str(_first(r, "SEM_EXM_EXCH_ID", "EXCH_ID", default="")).upper()
    seg = str(_first(r, "SEM_SEGMENT", "SEGMENT", default="")).upper()
    return {
        "security_id": str(_first(r, "SEM_SMST_SECURITY_ID", "SECURITY_ID", default="")),
        "exchange": exch, "segment_code": seg,
        "segment": _SEGMENT_ENUM.get((exch, seg), f"{exch}_{seg}".strip("_")),
        "instrument": str(_first(r, "SEM_INSTRUMENT_NAME", "INSTRUMENT", default="")).upper(),
        "trading_symbol": str(_first(r, "SEM_TRADING_SYMBOL", "SYMBOL_NAME", "SM_SYMBOL_NAME", default="")).upper(),
        "underlying": str(_first(r, "UNDERLYING_SYMBOL", default="")).upper(),
        "lot": _i(_first(r, "SEM_LOT_UNITS", "LOT_SIZE")),
        "expiry": str(_first(r, "SEM_EXPIRY_DATE", "SM_EXPIRY_DATE", default="") or "")[:10],
        "strike": _f(_first(r, "SEM_STRIKE_PRICE", "STRIKE_PRICE")),
        "opt": str(_first(r, "SEM_OPTION_TYPE", "OPTION_TYPE", default="") or "").upper(),
        "tick": _f(_first(r, "SEM_TICK_SIZE", "TICK_SIZE")) or 0.05,
    }


def _first(d: dict, *keys, default=None):
    """Tolerate key-name variation across API versions rather than KeyError."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
        lk = k.lower()
        for actual in d:
            if actual.lower() == lk and d[actual] not in (None, ""):
                return d[actual]
    return default


class DhanAdapter:
    """Read-only Dhan market data adapter.

    INVARIANT I1: there is no order-placement method here, and none may ever
    be added. `tests/test_invariant_no_trading.py` greps for the forbidden
    identifiers across the whole source tree.
    """

    name = "dhan"

    def __init__(self, client_id: str, access_token: str, limiter: RateLimiter,
                 timeout: float = 15.0):
        if not client_id or not access_token:
            raise BrokerError("Dhan credentials missing — set DHAN_CLIENT_ID and "
                              "DHAN_ACCESS_TOKEN in .env")
        self._client_id = client_id
        self._token = access_token
        self.limiter = limiter
        self._client = httpx.Client(
            base_url=API_BASE, timeout=timeout,
            headers={
                "access-token": access_token,
                "client-id": client_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._instrument_cache: list[Contract] = []
        self._instrument_cached_on: dt.date | None = None
        self._feed = None
        self._feed_thread: threading.Thread | None = None
        self._feed_stop = threading.Event()
        self._on_tick: TickCallback | None = None
        self._subscribed: dict[str, Contract] = {}
        self._last_tick_at: dt.datetime | None = None
        self._feed_connected = False
        self._lock = threading.Lock()

    # ── HTTP plumbing ─────────────────────────────────────────────────────
    def _post(self, endpoint: str, payload: dict, bucket: str,
              attempt: int = 1, max_attempts: int = 3) -> Any:
        if not self.limiter.acquire(bucket, timeout=30.0):
            raise RateLimitError(f"local rate limiter timed out for {bucket}")
        try:
            resp = self._client.post(endpoint, json=payload)
        except httpx.HTTPError as exc:
            raise BrokerError(f"network error calling {endpoint}: {type(exc).__name__}") from exc

        if resp.status_code == 429:
            delay = self.limiter.note_rate_limited(bucket, attempt)
            if attempt < max_attempts:
                import time
                time.sleep(delay)
                return self._post(endpoint, payload, bucket, attempt + 1, max_attempts)
            raise RateLimitError(f"{endpoint}: HTTP 429 after {attempt} attempts")

        if resp.status_code in (401, 403):
            raise self._auth_error(endpoint, resp)

        try:
            data = resp.json()
        except ValueError:
            raise BrokerError(f"{endpoint}: non-JSON response (HTTP {resp.status_code})") from None

        return self._unwrap(endpoint, data, bucket, attempt, max_attempts,
                            retry=lambda: self._post(endpoint, payload, bucket, attempt + 1, max_attempts))

    def _get(self, endpoint: str, params: dict | None = None, bucket: str = "account",
             attempt: int = 1, max_attempts: int = 3) -> Any:
        """GET plumbing for the free Trading-API reads (profile, funds, ledger...)."""
        if not self.limiter.acquire(bucket, timeout=30.0):
            raise RateLimitError(f"local rate limiter timed out for {bucket}")
        try:
            resp = self._client.get(endpoint, params=params or None)
        except httpx.HTTPError as exc:
            raise BrokerError(f"network error calling {endpoint}: {type(exc).__name__}") from exc
        if resp.status_code == 429:
            delay = self.limiter.note_rate_limited(bucket, attempt)
            if attempt < max_attempts:
                import time
                time.sleep(delay)
                return self._get(endpoint, params, bucket, attempt + 1, max_attempts)
            raise RateLimitError(f"{endpoint}: HTTP 429 after {attempt} attempts")
        if resp.status_code in (401, 403):
            raise self._auth_error(endpoint, resp)
        try:
            data = resp.json()
        except ValueError:
            raise BrokerError(f"{endpoint}: non-JSON response (HTTP {resp.status_code})") from None
        if resp.status_code >= 400 and not isinstance(data, dict):
            raise BrokerError(f"{endpoint}: HTTP {resp.status_code}")
        return self._unwrap(endpoint, data, bucket, attempt, max_attempts,
                            retry=lambda: self._get(endpoint, params, bucket, attempt + 1, max_attempts))

    @staticmethod
    def _auth_error(endpoint: str, resp: httpx.Response) -> Exception:
        """Classify a 401/403.

        v1 defect E3 was a wall of 401s on an expired token. The opposite
        mistake is just as misleading: Dhan ALSO answers 401 when a free-plan
        token calls a Data-API endpoint ({"data": {"806": "Data APIs not
        Subscribed"}} or errorCode DH-902). Read the body before blaming
        the token -- a user with a valid token was told to regenerate it.
        """
        try:
            body = resp.json()
        except ValueError:
            body = {}
        _status, code, msg = _error_fields(body)
        if _is_entitlement(code, msg):
            return DataEntitlementError(
                f"{endpoint}: {msg or 'Data API subscription required'} "
                f"(HTTP {resp.status_code}, code {code or '?'}). {_DATA_HELP}")
        return TokenExpiredError(
            f"{endpoint}: HTTP {resp.status_code} — {msg or 'the Dhan access token is invalid or expired'}. "
            "Generate a new one (web.dhan.co -> My Profile -> Access DhanHQ APIs) and update .env.")

    def _unwrap(self, endpoint: str, data: Any, bucket: str, attempt: int, max_attempts: int,
                retry) -> Any:
        """Map Dhan's error envelope to typed exceptions; return the payload."""
        if isinstance(data, dict):
            status, err_code, err_msg = _error_fields(data)
            if err_code.upper().startswith("RL") or err_code in ("DH-904", "805") or "rate" in err_msg.lower():
                delay = self.limiter.note_rate_limited(bucket, attempt)
                if attempt < max_attempts:
                    import time
                    time.sleep(delay)
                    return retry()
                raise RateLimitError(f"{endpoint}: {err_code} {err_msg}")
            if err_code in _TOKEN_CODES or "invalid token" in err_msg.lower() or "expired" in err_msg.lower():
                raise TokenExpiredError(f"{endpoint}: {err_code} {err_msg or 'token rejected'}")
            if _is_entitlement(err_code, err_msg):
                raise DataEntitlementError(
                    f"{endpoint}: {err_msg or 'Data API subscription required'}. {_DATA_HELP}")
            if status in ("failure", "failed") or (err_code and err_code.startswith("DH-")):
                raise BrokerError(f"{endpoint}: {err_code} {err_msg}")
            if "data" in data:
                return data["data"]
        return data

    # ── free Trading-API reads ────────────────────────────────────────────
    def profile(self) -> dict:
        """dhanClientId, tokenValidity, activeSegment, dataPlan, dataValidity, ddpi, mtf."""
        d = self._get(EP_PROFILE)
        return d if isinstance(d, dict) else {}

    def fund_limits(self) -> dict:
        d = self._get(EP_FUNDS)
        return d if isinstance(d, dict) else {}

    def holdings(self) -> list[dict]:
        d = self._get(EP_HOLDINGS)
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []

    def positions(self) -> list[dict]:
        d = self._get(EP_POSITIONS)
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []

    def ledger(self, start: dt.date, end: dt.date) -> list[dict]:
        d = self._get(EP_LEDGER, {"from-date": start.isoformat(), "to-date": end.isoformat()})
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []

    def trade_history(self, start: dt.date, end: dt.date, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        for page in range(max_pages):
            d = self._get(f"{EP_TRADES}/{start.isoformat()}/{end.isoformat()}/{page}")
            rows = [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
            if not rows:
                break
            out.extend(rows)
            if len(rows) < 20:   # Dhan pages are small; a short page is the last one
                break
        return out

    def data_api_status(self) -> dict:
        """Whether this account can use the paid Data API, from the profile."""
        p = self.profile()
        plan = str(p.get("dataPlan") or "").strip()
        return {"active": plan.lower() == "active", "plan": plan or None,
                "valid_until": p.get("dataValidity"), "token_valid_until": p.get("tokenValidity"),
                "segments": p.get("activeSegment")}

    def scrip_lookup(self, trading_symbol: str, segment: str | None = None,
                     instrument: str | None = None) -> list[dict]:
        """Rows of the scrip master matching a trading symbol (e.g. USDINR futures)."""
        sym = trading_symbol.upper()
        out = []
        for r in self._download_scrip_master():
            f = scrip_fields(r)
            if not (f["trading_symbol"].startswith(sym) or f["underlying"] == sym):
                continue
            if segment and f["segment"] != segment:
                continue
            if instrument and f["instrument"] != instrument.upper():
                continue
            out.append({**r, "_norm": f})
        return out

    # ── preflight ─────────────────────────────────────────────────────────
    def preflight(self, underlying_security_id: int = 25,
                  segment: str = "IDX_I") -> dict:
        """Exercise every endpoint and report exactly what works.

        Replaces v1's failure mode of discovering missing entitlements as an
        obscure error mid-session.
        """
        result: dict[str, Any] = {"broker": "dhan", "checks": {}, "ok": True, "notes": [],
                                  "trading_ok": True, "data_ok": True, "profile": None}

        def check(name: str, fn, tier: str):
            try:
                val = fn()
                result["checks"][name] = {"ok": True, "tier": tier}
                return val
            except TokenExpiredError as e:
                result["checks"][name] = {"ok": False, "tier": tier, "error": "TOKEN_EXPIRED", "detail": str(e)}
                result["ok"] = False
                result[f"{tier}_ok"] = False
                result["notes"].append("Access token is invalid or expired — regenerate it.")
            except DataEntitlementError as e:
                result["checks"][name] = {"ok": False, "tier": tier, "error": "NO_DATA_API", "detail": str(e)}
                result["ok"] = False
                result[f"{tier}_ok"] = False
                result["notes"].append(
                    "Dhan Data API subscription appears to be missing. The option chain, "
                    "historical bars and live feed require it; it is a paid add-on separate "
                    "from the free trading API.")
            except Exception as e:
                result["checks"][name] = {"ok": False, "tier": tier, "error": type(e).__name__,
                                          "detail": str(e)[:300]}
                result["ok"] = False
                result[f"{tier}_ok"] = False
            return None

        # Free tier first: these must work on any token.
        prof = check("profile", self.profile, "trading")
        if isinstance(prof, dict):
            result["profile"] = {"client_id_suffix": str(prof.get("dhanClientId") or "")[-4:],
                                 "token_validity": prof.get("tokenValidity"),
                                 "data_plan": prof.get("dataPlan"),
                                 "data_validity": prof.get("dataValidity"),
                                 "segments": prof.get("activeSegment")}
            if str(prof.get("dataPlan") or "").lower() != "active":
                result["notes"].append(
                    "Profile says the Data API plan is NOT active; data checks below will fail "
                    "until the subscription is bought.")
        check("fund_limits", self.fund_limits, "trading")
        check("holdings", self.holdings, "trading")
        check("positions", self.positions, "trading")
        today = dt.datetime.now(IST).date()
        check("ledger_30d", lambda: self.ledger(today - dt.timedelta(days=30), today), "trading")
        check("trade_history_30d", lambda: self.trade_history(today - dt.timedelta(days=30), today, 2), "trading")

        # Public reference data (no auth): the scrip master.
        check("scrip_master", lambda: self._download_scrip_master(limit=5), "data")

        # Paid tier: everything the live engine needs.
        check("expiry_list", lambda: self.expiry_list_raw(underlying_security_id, segment), "data")
        exp = None
        try:
            exp = (self.expiry_list_raw(underlying_security_id, segment) or [None])[0]
        except Exception:
            pass
        if exp:
            check("option_chain",
                  lambda: self._post(EP_OPTION_CHAIN, {
                      "UnderlyingScrip": underlying_security_id,
                      "UnderlyingSeg": segment, "Expiry": exp}, "option_chain"), "data")
        index_c = Contract(security_id=str(underlying_security_id), exchange_segment=segment,
                           symbol="INDEX", underlying="INDEX", instrument_type="INDEX")
        check("historical_daily",
              lambda: self.historical_daily(index_c, today - dt.timedelta(days=10), today), "data")
        check("intraday_5m",
              lambda: self.historical_intraday(
                  index_c, 5, dt.datetime.combine(today - dt.timedelta(days=5), dt.time(9, 0), tzinfo=IST),
                  dt.datetime.combine(today, dt.time(16, 0), tzinfo=IST)), "data")
        result["notes"] = list(dict.fromkeys(result["notes"]))  # dedupe, keep order
        return result

    # ── reference data ────────────────────────────────────────────────────
    def _download_scrip_master(self, limit: int | None = None) -> list[dict]:
        """Dhan's instrument master CSV.

        v1 defect D6: without this there are no securityIds to subscribe to,
        and — critically — no authoritative lot size or expiry date. Both have
        been revised by regulators; hardcoding either is a live landmine.
        """
        if not self.limiter.acquire("reference", timeout=60.0):
            raise RateLimitError("rate limiter timed out for scrip master")
        try:
            with httpx.Client(timeout=90.0) as c:
                resp = c.get(SCRIP_MASTER_URL)
                resp.raise_for_status()
                text = resp.text
        except httpx.HTTPError as exc:
            raise BrokerError(f"scrip master download failed: {type(exc).__name__}") from exc
        rows = list(csv.DictReader(io.StringIO(text)))
        return rows[:limit] if limit else rows

    def instruments(self, underlying: str = "BANKNIFTY",
                    force: bool = False) -> list[Contract]:
        today = dt.datetime.now(IST).date()
        if not force and self._instrument_cached_on == today and self._instrument_cache:
            return self._instrument_cache

        contracts: list[Contract] = []
        want = underlying.upper()
        for r in self._download_scrip_master():
            f = scrip_fields(r)
            if f["instrument"] not in ("OPTIDX", "FUTIDX", "INDEX"):
                continue
            if want not in (f["underlying"], f["trading_symbol"]) and not f["trading_symbol"].startswith(want + "-"):
                continue
            # "NIFTY" must not pull in BANKNIFTY / FINNIFTY / MIDCPNIFTY rows
            if f["underlying"] and f["underlying"] != want:
                continue
            if f["instrument"] == "INDEX" and f["segment"] != "IDX_I":
                continue
            contracts.append(Contract(
                security_id=f["security_id"], exchange_segment=f["segment"],
                symbol=f["trading_symbol"], underlying=want, instrument_type=f["instrument"],
                expiry=(f["expiry"] if f["expiry"] and not f["expiry"].startswith("0001") else None),
                strike=f["strike"] if f["strike"] and f["strike"] > 0 else None,
                opt_type=f["opt"] if f["opt"] in ("CE", "PE") else None,
                lot_size=f["lot"], tick_size=f["tick"],
            ))
        self._instrument_cache = contracts
        self._instrument_cached_on = today
        log.info("Loaded %d %s contracts from the scrip master", len(contracts), underlying)
        return contracts

    def expiry_list_raw(self, security_id: int, segment: str = "IDX_I") -> list[str]:
        data = self._post(EP_EXPIRY_LIST,
                          {"UnderlyingScrip": security_id, "UnderlyingSeg": segment},
                          "reference")
        if isinstance(data, dict):
            data = _first(data, "data", "expiry", default=[])
        return [str(x)[:10] for x in (data or [])]

    def expiry_list(self, underlying: str, security_id: int = 25,
                    segment: str = "IDX_I") -> list[str]:
        return self.expiry_list_raw(security_id, segment)

    # ── option chain ──────────────────────────────────────────────────────
    def option_chain(self, underlying: str, expiry: str, security_id: int = 25,
                     segment: str = "IDX_I") -> ChainSnapshot:
        data = self._post(EP_OPTION_CHAIN, {
            "UnderlyingScrip": security_id, "UnderlyingSeg": segment, "Expiry": expiry,
        }, "option_chain")

        now = dt.datetime.now(IST)
        under_price = _f(_first(data or {}, "last_price", "underlying_price", "ltp")) or 0.0
        oc = _first(data or {}, "oc", "optionChain", "option_chain", default={}) or {}

        rows: list[ChainRow] = []
        for strike_key, sides in oc.items():
            strike = _f(strike_key)
            if strike is None or not isinstance(sides, dict):
                continue
            for side_key, opt_type in (("ce", "CE"), ("pe", "PE")):
                leg = _first(sides, side_key, opt_type.lower(), opt_type, default=None)
                if not isinstance(leg, dict):
                    continue
                g = _first(leg, "greeks", default={}) or {}
                rows.append(ChainRow(
                    strike=strike, opt_type=opt_type,
                    security_id=str(_first(leg, "security_id", "securityId", default="") or ""),
                    ltp=_f(_first(leg, "last_price", "ltp")),
                    bid=_f(_first(leg, "top_bid_price", "bid_price", "bid")),
                    ask=_f(_first(leg, "top_ask_price", "ask_price", "ask")),
                    bid_qty=_i(_first(leg, "top_bid_quantity", "bid_quantity")),
                    ask_qty=_i(_first(leg, "top_ask_quantity", "ask_quantity")),
                    volume=_f(_first(leg, "volume", "traded_volume")),
                    oi=_f(_first(leg, "oi", "open_interest")),
                    iv=_f(_first(leg, "implied_volatility", "iv")),
                    delta=_f(_first(g, "delta")), gamma=_f(_first(g, "gamma")),
                    theta=_f(_first(g, "theta")), vega=_f(_first(g, "vega")),
                    greek_source="broker",
                ))
        # Dhan reports IV in percent; the pricing model works in decimals.
        for r in rows:
            if r.iv is not None and r.iv > 1.5:
                r.iv = r.iv / 100.0
        return ChainSnapshot(ts=now, underlying=underlying, expiry=expiry,
                             underlying_price=under_price, rows=rows)

    # ── quotes (WebSocket failover path) ──────────────────────────────────
    def quote(self, contracts: list[Contract]) -> dict[str, QuoteSnapshot]:
        if not contracts:
            return {}
        by_segment: dict[str, list[int]] = {}
        for c in contracts:
            by_segment.setdefault(c.exchange_segment, []).append(int(c.security_id))
        data = self._post(EP_QUOTE, by_segment, "quote")
        now = dt.datetime.now(IST)
        out: dict[str, QuoteSnapshot] = {}
        for _seg, items in (data or {}).items():
            if not isinstance(items, dict):
                continue
            for sid, q in items.items():
                if not isinstance(q, dict):
                    continue
                depth = _first(q, "depth", default={}) or {}
                buy = (_first(depth, "buy", default=[]) or [{}])[0]
                sell = (_first(depth, "sell", default=[]) or [{}])[0]
                out[str(sid)] = QuoteSnapshot(
                    security_id=str(sid), ts=now,
                    ltp=_f(_first(q, "last_price", "ltp")),
                    bid=_f(_first(buy, "price", "bid_price")),
                    ask=_f(_first(sell, "price", "ask_price")),
                    bid_qty=_i(_first(buy, "quantity")),
                    ask_qty=_i(_first(sell, "quantity")),
                    volume=_f(_first(q, "volume")), oi=_f(_first(q, "oi")),
                    source="rest",
                )
        return out

    # ── historical ────────────────────────────────────────────────────────
    def _bars_from_payload(self, data: dict, tz_naive_is_ist: bool = True) -> list[Bar]:
        if not isinstance(data, dict):
            return []
        ts = _first(data, "timestamp", "start_Time", "time", default=[]) or []
        o = _first(data, "open", default=[]) or []
        h = _first(data, "high", default=[]) or []
        low = _first(data, "low", default=[]) or []
        c = _first(data, "close", default=[]) or []
        v = _first(data, "volume", default=[]) or []
        out: list[Bar] = []
        for i in range(min(len(ts), len(o), len(h), len(low), len(c))):
            try:
                epoch = float(ts[i])
                when = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).astimezone(IST)
            except (TypeError, ValueError, OSError):
                try:
                    when = to_ist(dt.datetime.fromisoformat(str(ts[i])))
                except ValueError:
                    continue
            vol = _f(v[i]) if i < len(v) else None
            # An index reports volume 0. Store NULL so VWAP cannot silently
            # compute on 0/0 (v1 defect B3).
            out.append(Bar(ts=when, open=float(o[i]), high=float(h[i]),
                           low=float(low[i]), close=float(c[i]),
                           volume=vol if vol else None))
        return out

    def historical_daily(self, contract: Contract, start: dt.date, end: dt.date) -> list[Bar]:
        data = self._post(EP_HISTORICAL, {
            "securityId": str(contract.security_id),
            "exchangeSegment": contract.exchange_segment,
            "instrument": contract.instrument_type,
            "expiryCode": 0,
            "fromDate": start.isoformat(), "toDate": end.isoformat(),
        }, "historical")
        return self._bars_from_payload(data)

    def historical_intraday(self, contract: Contract, interval_min: int,
                            start: dt.datetime, end: dt.datetime) -> list[Bar]:
        data = self._post(EP_INTRADAY, {
            "securityId": str(contract.security_id),
            "exchangeSegment": contract.exchange_segment,
            "instrument": contract.instrument_type,
            "interval": str(interval_min),
            "fromDate": start.date().isoformat(), "toDate": end.date().isoformat(),
        }, "historical")
        bars = self._bars_from_payload(data)
        return [b for b in bars if start <= b.ts <= end]

    # ── WebSocket feed ────────────────────────────────────────────────────
    def start_stream(self, contracts: list[Contract], on_tick: TickCallback) -> None:
        """Start the live feed. Falls back to REST polling if the SDK feed is
        unavailable — a missing optional dependency must not stop the engine."""
        self._on_tick = on_tick
        with self._lock:
            for c in contracts:
                self._subscribed[str(c.security_id)] = c
        try:
            from dhanhq import marketfeed  # noqa: F401
        except ImportError:
            log.warning("dhanhq marketfeed unavailable — running on REST polling only. "
                        "Health will report DEGRADED.")
            self._feed_connected = False
            return
        self._feed_stop.clear()
        self._feed_thread = threading.Thread(
            target=self._feed_loop, name="dhan-feed", daemon=True)
        self._feed_thread.start()

    # dhanhq >= 2.x MarketFeed wants integer segment codes in its tuples.
    _WS_SEGMENT = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2, "NSE_CURRENCY": 3, "BSE_EQ": 4,
                   "MCX_COMM": 5, "BSE_CURRENCY": 7, "BSE_FNO": 8}

    def _ws_tuples(self, contracts) -> list[tuple]:
        from dhanhq import marketfeed

        out = []
        for c in contracts:
            seg = self._WS_SEGMENT.get(str(c.exchange_segment))
            if seg is None:
                log.warning("feed: unknown exchange segment %s for %s", c.exchange_segment, c.security_id)
                continue
            out.append((seg, str(c.security_id), marketfeed.MarketFeed.Quote))
        return out

    def _feed_loop(self) -> None:  # pragma: no cover - requires live credentials
        """Reconnecting feed loop with jittered exponential backoff.

        v1 defect D4: v1 treated 3 seconds of tick silence as a dead socket.
        Ticks arrive on TRADES — a quiet strike legitimately goes 10s+ without
        one. Liveness is a property of the CONNECTION, not of tick arrival.

        Uses the dhanhq 2.x `MarketFeed(DhanContext, instruments, "v2")` API:
        `run_forever()` connects + subscribes, `get_data()` yields one message.
        (The previous build called a `DhanFeed` class that no longer exists, so
        the socket never connected and the engine silently lived on REST.)
        """
        import random
        import time

        from dhanhq import DhanContext, marketfeed

        attempt = 0
        while not self._feed_stop.is_set():
            try:
                with self._lock:
                    subs = self._ws_tuples(self._subscribed.values())
                if not subs:
                    self._feed_connected = False
                    time.sleep(5.0)
                    continue
                ctx = DhanContext(self._client_id, self._token)
                feed = marketfeed.MarketFeed(ctx, subs, "v2")
                self._feed = feed
                feed.run_forever()                       # connect + subscribe
                self._feed_connected = True
                attempt = 0
                log.info("Dhan market feed connected (%d instruments)", len(subs))
                while not self._feed_stop.is_set():
                    msg = feed.get_data()
                    if msg:
                        self._handle_feed_message(msg)
                        if isinstance(msg, dict) and msg.get("type") == "Disconnect":
                            raise ConnectionError(f"server disconnect {msg}")
            except Exception as exc:
                self._feed_connected = False
                attempt += 1
                delay = min(60.0, 2.0 * (2 ** min(attempt, 5))) * (0.5 + random.random())
                log.warning("Market feed disconnected (%s: %s) — reconnecting in %.1fs",
                            type(exc).__name__, str(exc)[:120], delay)
                try:
                    if self._feed is not None:
                        self._feed.close_connection()
                except Exception:
                    pass
                self._feed = None
                self._feed_stop.wait(delay)
        self._feed_connected = False

    def _handle_feed_message(self, msg: dict) -> None:  # pragma: no cover
        if not isinstance(msg, dict):
            return
        sid = str(_first(msg, "security_id", "securityId", default="") or "")
        if not sid or not self._on_tick:
            return
        ltp = _f(_first(msg, "LTP", "last_price", "ltp"))
        if ltp is None:
            return                                   # OI / prev-close / status packets
        now = dt.datetime.now(IST)
        self._last_tick_at = now
        self._on_tick(QuoteSnapshot(
            security_id=sid, ts=now, ltp=ltp,
            bid=_f(_first(msg, "bid_price", "top_bid_price")),
            ask=_f(_first(msg, "ask_price", "top_ask_price")),
            bid_qty=_i(_first(msg, "bid_quantity")),
            ask_qty=_i(_first(msg, "ask_quantity")),
            volume=_f(_first(msg, "volume")), oi=_f(_first(msg, "OI", "oi")),
            source="ws",
        ))

    def update_subscription(self, contracts: list[Contract]) -> None:
        with self._lock:
            new = [c for c in contracts if str(c.security_id) not in self._subscribed]
            for c in new:
                self._subscribed[str(c.security_id)] = c
        if new and self._feed is not None:  # pragma: no cover
            try:
                self._feed.subscribe_symbols(self._ws_tuples(new))
            except Exception as exc:
                log.warning("Could not add subscriptions live (%s); "
                            "they apply on next reconnect.", type(exc).__name__)

    def unsubscribe(self, contracts: list[Contract]) -> None:
        with self._lock:
            gone = [self._subscribed.pop(str(c.security_id), None) for c in contracts]
        gone = [c for c in gone if c]
        if gone and self._feed is not None:  # pragma: no cover
            try:
                self._feed.unsubscribe_symbols(self._ws_tuples(gone))
            except Exception as exc:
                log.warning("Could not drop subscriptions live (%s)", type(exc).__name__)

    def subscribed_ids(self) -> set[str]:
        with self._lock:
            return set(self._subscribed)

    def set_token(self, access_token: str) -> None:
        """Swap in a renewed token: REST headers now, the socket on its next (forced) reconnect."""
        self._token = access_token
        self._client.headers["access-token"] = access_token
        if self._feed is not None:  # pragma: no cover
            try:
                self._feed.close_connection()
            except Exception:
                pass

    def stop_stream(self) -> None:
        self._feed_stop.set()
        if self._feed is not None:  # pragma: no cover
            try:
                self._feed.disconnect()
            except Exception:
                pass
        self._feed_connected = False

    def stream_alive(self) -> bool:
        """CONNECTION liveness, not tick recency. See _feed_loop."""
        return self._feed_connected

    def last_tick_at(self) -> dt.datetime | None:
        return self._last_tick_at

    def close(self) -> None:
        self.stop_stream()
        self._client.close()
