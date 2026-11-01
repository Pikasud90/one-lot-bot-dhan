"""Free-tier Dhan reads and the cached account service.

The HTTP layer is exercised against an httpx MockTransport, so every error
mapping (token, entitlement, rate limit) is verified without a network.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from onelot.broker.account import KEYS, AccountService
from onelot.broker.base import DataEntitlementError, RateLimitError, TokenExpiredError
from onelot.broker.dhan import API_BASE, DhanAdapter
from onelot.ratelimit import RateLimiter
from onelot.store import init_database


def make_adapter(handler) -> DhanAdapter:
    a = DhanAdapter("1100001234", "eyJ" + "x" * 40, RateLimiter())
    a._client.close()
    a._client = httpx.Client(
        base_url=API_BASE,
        transport=httpx.MockTransport(handler),
        headers={"access-token": "t", "client-id": "c"},
    )
    return a


PROFILE = {
    "dhanClientId": "1100001234",
    "tokenValidity": "28/10/2026 15:37",
    "activeSegment": "Equity, Derivative",
    "ddpi": "Active",
    "mtf": "Deactive",
    "dataPlan": "Deactive",
    "dataValidity": None,
}
FUNDS = {
    "dhanClientId": "1100001234",
    "availabelBalance": 98765.5,
    "sodLimit": 100000,
    "collateralAmount": 0,
    "receiveableAmount": 0,
    "utilizedAmount": 1234.5,
    "blockedPayoutAmount": 0,
    "withdrawableBalance": 97000,
}
# Dhan returns the ledger NEWEST first, with every number as a string.
LEDGER = [
    {
        "narration": "Brokerage",
        "voucherdate": "Oct 02, 2026",
        "debit": "23.6",
        "credit": "0.00",
        "runbal": "99976.4",
    },
    {
        "narration": "Opening",
        "voucherdate": "Oct 01, 2026",
        "debit": "0.00",
        "credit": "100000",
        "runbal": "100000",
    },
]
# The two 401 bodies a FREE-plan token gets from Data-API endpoints (captured live, 2026-10-27).
NO_DATA_806 = {"data": {"806": "Data APIs not Subscribed"}, "status": "failed"}
NO_DATA_902 = {
    "errorType": "Invalid_Access",
    "errorCode": "DH-902",
    "errorMessage": "This API requires a Data APIs Plan. Current plan: Free Plan",
}
TRADES_P0 = [
    {
        "orderId": "1",
        "tradingSymbol": "SBIN",
        "transactionType": "BUY",
        "tradedQuantity": 1,
        "tradedPrice": 800.0,
        "brokerageCharges": 0.24,
        "stt": 0,
        "exchangeTransactionCharges": 0.02,
        "sebiTax": 0.0,
        "stampDuty": 0.02,
        "serviceTax": 0.05,
        "exchangeTime": "2026-10-20 10:15:00",
    }
]


def good_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p.endswith("/profile"):
        return httpx.Response(200, json=PROFILE)
    if p.endswith("/fundlimit"):
        return httpx.Response(200, json=FUNDS)
    if p.endswith("/holdings"):
        return httpx.Response(
            200,
            json=[
                {
                    "tradingSymbol": "SBIN",
                    "exchange": "ALL",
                    "totalQty": "2",
                    "availableQty": "1",
                    "avgCostPrice": "780.0",
                    "lastTradedPrice": "800.0",
                    "securityId": "3045",
                }
            ],
        )
    if p.endswith("/positions"):
        return httpx.Response(
            200,
            json=[
                {
                    "tradingSymbol": "BANKNIFTY-Oct2026-50000-CE",
                    "netQty": 35,
                    "buyAvg": 500,
                    "sellAvg": 0,
                    "realizedProfit": 0,
                    "unrealizedProfit": 1750,
                    "productType": "INTRADAY",
                    "exchangeSegment": "NSE_FNO",
                }
            ],
        )
    if p.endswith("/ledger"):
        assert request.url.params["from-date"] < request.url.params["to-date"]
        return httpx.Response(200, json=LEDGER)
    if "/trades/" in p:
        page = int(p.rsplit("/", 1)[1])
        return httpx.Response(200, json=TRADES_P0 if page == 0 else [])
    return httpx.Response(404, json={"errorCode": "DH-905", "errorMessage": "no such endpoint"})


class TestFreeTierReads:
    def test_all_free_endpoints_parse(self):
        a = make_adapter(good_handler)
        assert a.profile()["dhanClientId"] == "1100001234"
        assert a.fund_limits()["availabelBalance"] == 98765.5
        assert a.holdings()[0]["tradingSymbol"] == "SBIN"
        assert a.positions()[0]["netQty"] == 35
        today = dt.date(2026, 10, 27)
        assert len(a.ledger(today - dt.timedelta(days=30), today)) == 2
        assert len(a.trade_history(today - dt.timedelta(days=30), today)) == 1
        st = a.data_api_status()
        assert st["active"] is False and st["plan"] == "Deactive"

    def test_free_reads_are_gets_and_never_post(self):
        seen = []

        def h(req):
            seen.append(req.method)
            return good_handler(req)

        a = make_adapter(h)
        for fn in (a.profile, a.fund_limits, a.holdings, a.positions):
            fn()
        assert set(seen) == {"GET"}

    def test_token_rejection_maps_to_token_expired(self):
        a = make_adapter(
            lambda r: httpx.Response(
                401, json={"errorCode": "DH-901", "errorMessage": "Invalid token"}
            )
        )
        with pytest.raises(TokenExpiredError):
            a.profile()
        a2 = make_adapter(
            lambda r: httpx.Response(
                200, json={"errorCode": "DH-901", "errorMessage": "token expired"}
            )
        )
        with pytest.raises(TokenExpiredError):
            a2.fund_limits()

    def test_data_api_missing_maps_to_entitlement_error(self):
        a = make_adapter(
            lambda r: httpx.Response(
                200, json={"errorCode": "DH-902", "errorMessage": "Data APIs not subscribed"}
            )
        )
        with pytest.raises(DataEntitlementError):
            a.expiry_list_raw(25, "IDX_I")
        a2 = make_adapter(
            lambda r: httpx.Response(
                200, json={"errorCode": "806", "errorMessage": "Data APIs not subscribed"}
            )
        )
        with pytest.raises(DataEntitlementError):
            a2.historical_daily(_index_contract(), dt.date(2026, 10, 1), dt.date(2026, 10, 10))

    def test_401_with_entitlement_body_is_not_a_token_problem(self):
        """Live finding: a valid free-plan token gets HTTP 401 from Data-API
        endpoints. Blaming the token sent the user to regenerate a good one."""
        a = make_adapter(lambda r: httpx.Response(401, json=NO_DATA_806))
        with pytest.raises(DataEntitlementError) as ei:
            a.expiry_list_raw(25, "IDX_I")
        assert "806" in str(ei.value) and "Rs 499" in str(ei.value)
        a2 = make_adapter(lambda r: httpx.Response(401, json=NO_DATA_902))
        with pytest.raises(DataEntitlementError):
            a2.historical_daily(_index_contract(), dt.date(2026, 10, 1), dt.date(2026, 10, 10))
        # a bare 401 (no body) is still a token problem
        a3 = make_adapter(lambda r: httpx.Response(401, text="Unauthorized"))
        with pytest.raises(TokenExpiredError):
            a3.profile()

    def test_failed_envelope_with_code_as_key_on_200(self):
        a = make_adapter(lambda r: httpx.Response(200, json=NO_DATA_806))
        with pytest.raises(DataEntitlementError):
            a.expiry_list_raw(25, "IDX_I")

    def test_rate_limit_retries_then_raises(self):
        calls = {"n": 0}

        def h(req):
            calls["n"] += 1
            return httpx.Response(429, json={"errorCode": "DH-904", "errorMessage": "rate"})

        a = make_adapter(h)
        a.limiter.note_rate_limited = lambda *_a, **_k: 0.0  # no real sleeping in tests
        with pytest.raises(RateLimitError):
            a.profile()
        assert calls["n"] == 3

    @pytest.mark.parametrize("body", [NO_DATA_806, NO_DATA_902])
    def test_preflight_separates_trading_from_data_tier(self, body):
        def h(req):
            if (
                req.url.path.endswith(
                    ("/profile", "/fundlimit", "/holdings", "/positions", "/ledger")
                )
                or "/trades/" in req.url.path
            ):
                return good_handler(req)
            return httpx.Response(401, json=body)  # exactly what the free plan gets

        a = make_adapter(h)
        a._download_scrip_master = lambda limit=None: [{"SEM_SMST_SECURITY_ID": "25"}]
        res = a.preflight(25, "IDX_I")
        assert res["trading_ok"] is True and res["data_ok"] is False
        tiers = {k: v["tier"] for k, v in res["checks"].items()}
        assert tiers["profile"] == "trading" and tiers["expiry_list"] == "data"
        assert res["profile"]["data_plan"] == "Deactive"
        assert any("NOT active" in n for n in res["notes"])
        failed = {k: v for k, v in res["checks"].items() if not v["ok"]}
        assert failed and all(v["error"] == "NO_DATA_API" for v in failed.values())
        assert not any("regenerate" in n for n in res["notes"])  # the token is fine
        assert len(res["notes"]) == len(set(res["notes"]))  # deduped


def _index_contract():
    from onelot.broker.base import Contract

    return Contract(
        security_id="25",
        exchange_segment="IDX_I",
        symbol="BANKNIFTY",
        underlying="BANKNIFTY",
        instrument_type="INDEX",
    )


class TestAccountService:
    @pytest.fixture()
    def engine(self, tmp_path):
        db = init_database(f"sqlite:///{tmp_path / 'acct.db'}")
        yield db.engine
        db.dispose()

    def test_snapshot_without_adapter_is_unconfigured_not_an_error(self, engine):
        svc = AccountService(engine, adapter=None)
        snap = svc.snapshot()
        assert snap["configured"] is False
        assert all(p["data"] is None for p in snap["panels"].values())

    def test_refresh_caches_every_panel_and_builds_summary(self, engine):
        svc = AccountService(engine, adapter=make_adapter(good_handler))
        res = svc.refresh()
        assert all(v["ok"] for v in res.values()) and set(res) == set(KEYS)
        snap = svc.snapshot()
        s = snap["summary"]
        assert s["available_balance"] == 98765.5
        assert s["open_positions"] == 1 and s["positions_unrealized"] == 1750
        assert s["holdings_count"] == 1 and s["holdings_invested"] == 1560.0
        assert s["holdings_value"] == 1600.0 and s["holdings_pnl"] == pytest.approx(40.0)
        assert s["ledger_running_balance"] == 99976.4  # newest voucher, not last row
        assert s["ledger_last_date"] == "2026-10-02"
        assert s["ledger_debits"] == pytest.approx(23.6) and s["ledger_credits"] == pytest.approx(
            100000
        )
        assert s["data_plan"] == "Deactive" and s["client_id_suffix"] == "1234"
        assert snap["stale"] is False

    def test_snapshot_never_hits_the_broker_unless_asked(self, engine):
        calls = {"n": 0}

        def h(req):
            calls["n"] += 1
            return good_handler(req)

        svc = AccountService(engine, adapter=make_adapter(h))
        svc.snapshot()
        assert calls["n"] == 0
        svc.snapshot(refresh=True)
        assert calls["n"] > 0
        n = calls["n"]
        svc.snapshot()
        svc.snapshot()
        assert calls["n"] == n

    def test_errors_are_cached_per_panel(self, engine):
        def h(req):
            if req.url.path.endswith("/ledger"):
                return httpx.Response(
                    200, json={"errorCode": "DH-905", "errorMessage": "bad dates"}
                )
            return good_handler(req)

        svc = AccountService(engine, adapter=make_adapter(h))
        res = svc.refresh()
        assert res["ledger"]["ok"] is False and res["funds"]["ok"] is True
        snap = svc.snapshot()
        assert snap["panels"]["ledger"]["error"].startswith("BrokerError")
        assert snap["panels"]["funds"]["data"]["availabelBalance"] == 98765.5

    def test_failed_refresh_keeps_last_good_data(self, engine):
        """Day-two scenario: token expired / laptop offline. The Home tab must
        keep showing yesterday's ledger, clearly marked, never zeros."""
        svc = AccountService(engine, adapter=make_adapter(good_handler))
        svc.refresh()
        before = svc.snapshot()
        assert before["summary"]["available_balance"] == 98765.5 and before["last_error"] is None

        svc.adapter = make_adapter(
            lambda r: httpx.Response(401, json={"errorCode": "DH-901", "errorMessage": "expired"})
        )
        res = svc.refresh()
        assert all(v["error"] == "TOKEN_EXPIRED" for v in res.values())
        after = svc.snapshot()
        assert after["summary"]["available_balance"] == 98765.5  # data preserved
        assert after["panels"]["funds"]["data"]["availabelBalance"] == 98765.5
        assert after["panels"]["funds"]["fetched_at"] == before["panels"]["funds"]["fetched_at"]
        assert after["panels"]["funds"]["error"].startswith("TOKEN_EXPIRED")
        assert after["panels"]["funds"]["error_at"] is not None
        assert after["token_expired"] is True and after["last_error"]["panel"] in KEYS

        def offline(_r):
            raise httpx.ConnectError("no route to host")

        svc.adapter = make_adapter(offline)
        svc.refresh()
        snap = svc.snapshot()
        assert snap["offline"] is True and snap["summary"]["holdings_count"] == 1

    def test_summary_tolerates_strings_and_missing_fields(self):
        panels = {
            "funds": {"data": {"availabelBalance": "12.5"}},
            "positions": {"data": [{"netQty": "0"}, {"netQty": "-35", "unrealizedProfit": "-10"}]},
            "holdings": {"data": []},
            "profile": {"data": {}},
            "ledger": {"data": "not-a-list"},
        }
        s = AccountService._summary(panels)
        assert (
            s["available_balance"] == 12.5
            and s["open_positions"] == 1
            and s["positions_unrealized"] == -10
        )
        assert s["holdings_value"] is None and s["ledger_running_balance"] is None

    def test_ledger_order_does_not_matter(self):
        oldest_first = list(reversed(LEDGER))
        a = AccountService._summary({"ledger": {"data": oldest_first}})
        b = AccountService._summary({"ledger": {"data": LEDGER}})
        assert a["ledger_running_balance"] == b["ledger_running_balance"] == 99976.4

    def test_json_round_trip_of_cached_payloads(self, engine):
        svc = AccountService(engine, adapter=make_adapter(good_handler))
        svc.refresh()
        json.dumps(svc.snapshot())  # must be serialisable for the API


# ── scrip master: both layouts, real rows captured 2026-10-30 ──────────────────────────────
DETAILED_ROWS = [
    {
        "EXCH_ID": "NSE",
        "SEGMENT": "I",
        "SECURITY_ID": "25",
        "INSTRUMENT": "INDEX",
        "UNDERLYING_SECURITY_ID": "25",
        "UNDERLYING_SYMBOL": "BANKNIFTY",
        "SYMBOL_NAME": "BANKNIFTY",
        "INSTRUMENT_TYPE": "INDEX",
        "LOT_SIZE": "1.0",
        "SM_EXPIRY_DATE": "0001-01-01",
        "STRIKE_PRICE": "",
        "OPTION_TYPE": "XX",
        "TICK_SIZE": "0.0500",
    },
    {
        "EXCH_ID": "NSE",
        "SEGMENT": "D",
        "SECURITY_ID": "61466",
        "INSTRUMENT": "FUTIDX",
        "UNDERLYING_SECURITY_ID": "26009",
        "UNDERLYING_SYMBOL": "BANKNIFTY",
        "SYMBOL_NAME": "BANKNIFTY-Nov2026-FUT",
        "INSTRUMENT_TYPE": "FUT",
        "LOT_SIZE": "30.0",
        "SM_EXPIRY_DATE": "2026-11-23",
        "STRIKE_PRICE": "-0.01000",
        "OPTION_TYPE": "XX",
        "TICK_SIZE": "20.0000",
    },
    {
        "EXCH_ID": "NSE",
        "SEGMENT": "D",
        "SECURITY_ID": "35000",
        "INSTRUMENT": "OPTIDX",
        "UNDERLYING_SECURITY_ID": "26009",
        "UNDERLYING_SYMBOL": "BANKNIFTY",
        "SYMBOL_NAME": "BANKNIFTY-Nov2026-69400-CE",
        "INSTRUMENT_TYPE": "OP",
        "LOT_SIZE": "30.0",
        "SM_EXPIRY_DATE": "2026-11-23",
        "STRIKE_PRICE": "69400.00000",
        "OPTION_TYPE": "CE",
        "TICK_SIZE": "5.0000",
    },
    {
        "EXCH_ID": "NSE",
        "SEGMENT": "D",
        "SECURITY_ID": "99001",
        "INSTRUMENT": "OPTIDX",
        "UNDERLYING_SECURITY_ID": "26037",
        "UNDERLYING_SYMBOL": "FINNIFTY",
        "SYMBOL_NAME": "FINNIFTY-Nov2026-25000-PE",
        "INSTRUMENT_TYPE": "OP",
        "LOT_SIZE": "60.0",
        "SM_EXPIRY_DATE": "2026-11-23",
        "STRIKE_PRICE": "25000.00000",
        "OPTION_TYPE": "PE",
        "TICK_SIZE": "5.0000",
    },
    {
        "EXCH_ID": "NSE",
        "SEGMENT": "E",
        "SECURITY_ID": "2885",
        "INSTRUMENT": "EQUITY",
        "UNDERLYING_SYMBOL": "RELIANCE",
        "SYMBOL_NAME": "RELIANCE INDUSTRIES LTD",
        "INSTRUMENT_TYPE": "ES",
        "LOT_SIZE": "1.0",
        "SM_EXPIRY_DATE": "",
        "TICK_SIZE": "10.0",
    },
]
COMPACT_ROWS = [
    {
        "SEM_EXM_EXCH_ID": "NSE",
        "SEM_SEGMENT": "D",
        "SEM_SMST_SECURITY_ID": "61466",
        "SEM_INSTRUMENT_NAME": "FUTIDX",
        "SEM_TRADING_SYMBOL": "BANKNIFTY-Nov2026-FUT",
        "SEM_LOT_UNITS": "30.0",
        "SEM_EXPIRY_DATE": "2026-11-23 14:30:00",
        "SEM_STRIKE_PRICE": "-0.01000",
        "SEM_OPTION_TYPE": "XX",
        "SEM_TICK_SIZE": "20.0000",
    },
]


class TestScripMaster:
    def _adapter(self, rows):
        a = make_adapter(good_handler)
        a._download_scrip_master = lambda limit=None: rows
        a._instrument_cache, a._instrument_cached_on = [], None
        return a

    def test_detailed_layout_resolves_index_future_and_options(self):
        cs = self._adapter(DETAILED_ROWS).instruments("BANKNIFTY", force=True)
        kinds = {c.instrument_type for c in cs}
        assert kinds == {"INDEX", "FUTIDX", "OPTIDX"}  # derivatives were invisible before
        idx = next(c for c in cs if c.instrument_type == "INDEX")
        assert idx.exchange_segment == "IDX_I" and idx.security_id == "25" and idx.expiry is None
        fut = next(c for c in cs if c.instrument_type == "FUTIDX")
        assert (
            fut.lot_size == 30 and fut.expiry == "2026-11-23" and fut.exchange_segment == "NSE_FNO"
        )
        opt = next(c for c in cs if c.instrument_type == "OPTIDX")
        assert opt.opt_type == "CE" and opt.strike == 69400.0 and opt.lot_size == 30
        assert all(c.underlying == "BANKNIFTY" for c in cs)  # FINNIFTY row excluded

    def test_underlying_prefix_does_not_leak_between_indices(self):
        cs = self._adapter(DETAILED_ROWS).instruments("FINNIFTY", force=True)
        assert [c.security_id for c in cs] == ["99001"]
        assert self._adapter(DETAILED_ROWS).instruments("NIFTY", force=True) == []

    def test_compact_layout_still_works(self):
        cs = self._adapter(COMPACT_ROWS).instruments("BANKNIFTY", force=True)
        assert (
            len(cs) == 1
            and cs[0].lot_size == 30
            and cs[0].expiry == "2026-11-23"
            and cs[0].exchange_segment == "NSE_FNO"
        )

    def test_segment_enum_and_lookup(self):
        from onelot.broker.dhan import scrip_fields

        assert scrip_fields(DETAILED_ROWS[4])["segment"] == "NSE_EQ"
        assert scrip_fields({"EXCH_ID": "BSE", "SEGMENT": "I"})["segment"] == "IDX_I"
        a = self._adapter(DETAILED_ROWS)
        hit = a.scrip_lookup("BANKNIFTY", segment="NSE_FNO", instrument="FUTIDX")
        assert len(hit) == 1 and hit[0]["SECURITY_ID"] == "61466"
