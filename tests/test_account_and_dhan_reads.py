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
    a._client = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler),
                             headers={"access-token": "t", "client-id": "c"})
    return a


PROFILE = {"dhanClientId": "1100001234", "tokenValidity": "28/10/2026 15:37", "activeSegment": "Equity, Derivative",
           "ddpi": "Active", "mtf": "Deactive", "dataPlan": "Deactive", "dataValidity": None}
FUNDS = {"dhanClientId": "1100001234", "availabelBalance": 98765.5, "sodLimit": 100000, "collateralAmount": 0,
         "receiveableAmount": 0, "utilizedAmount": 1234.5, "blockedPayoutAmount": 0, "withdrawableBalance": 97000}
LEDGER = [{"narration": "Opening", "voucherdate": "Oct 01, 2026", "debit": "0", "credit": "100000", "runbal": "100000"},
          {"narration": "Brokerage", "voucherdate": "Oct 02, 2026", "debit": "23.6", "credit": "0", "runbal": "99976.4"}]
TRADES_P0 = [{"orderId": "1", "tradingSymbol": "SBIN", "transactionType": "BUY", "tradedQuantity": 1, "tradedPrice": 800.0,
              "brokerageCharges": 0.24, "stt": 0, "exchangeTransactionCharges": 0.02, "sebiTax": 0.0, "stampDuty": 0.02,
              "serviceTax": 0.05, "exchangeTime": "2026-10-20 10:15:00"}]


def good_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p.endswith("/profile"):
        return httpx.Response(200, json=PROFILE)
    if p.endswith("/fundlimit"):
        return httpx.Response(200, json=FUNDS)
    if p.endswith("/holdings"):
        return httpx.Response(200, json=[{"tradingSymbol": "SBIN", "exchange": "NSE", "totalQty": 1, "availableQty": 1,
                                          "avgCostPrice": 780.0, "securityId": "3045"}])
    if p.endswith("/positions"):
        return httpx.Response(200, json=[{"tradingSymbol": "BANKNIFTY-Oct2026-50000-CE", "netQty": 35, "buyAvg": 500,
                                          "sellAvg": 0, "realizedProfit": 0, "unrealizedProfit": 1750, "productType": "INTRADAY",
                                          "exchangeSegment": "NSE_FNO"}])
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
        a = make_adapter(lambda r: httpx.Response(401, json={"errorCode": "DH-901", "errorMessage": "Invalid token"}))
        with pytest.raises(TokenExpiredError):
            a.profile()
        a2 = make_adapter(lambda r: httpx.Response(200, json={"errorCode": "DH-901", "errorMessage": "token expired"}))
        with pytest.raises(TokenExpiredError):
            a2.fund_limits()

    def test_data_api_missing_maps_to_entitlement_error(self):
        a = make_adapter(lambda r: httpx.Response(200, json={"errorCode": "DH-902", "errorMessage": "Data APIs not subscribed"}))
        with pytest.raises(DataEntitlementError):
            a.expiry_list_raw(25, "IDX_I")
        a2 = make_adapter(lambda r: httpx.Response(200, json={"errorCode": "806", "errorMessage": "Data APIs not subscribed"}))
        with pytest.raises(DataEntitlementError):
            a2.historical_daily(_index_contract(), dt.date(2026, 10, 1), dt.date(2026, 10, 10))

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

    def test_preflight_separates_trading_from_data_tier(self):
        def h(req):
            if req.url.path.endswith(("/profile", "/fundlimit", "/holdings", "/positions", "/ledger")) or "/trades/" in req.url.path:
                return good_handler(req)
            return httpx.Response(200, json={"errorCode": "DH-902", "errorMessage": "Data APIs not subscribed"})

        a = make_adapter(h)
        a._download_scrip_master = lambda limit=None: [{"SEM_SMST_SECURITY_ID": "25"}]
        res = a.preflight(25, "IDX_I")
        assert res["trading_ok"] is True and res["data_ok"] is False
        tiers = {k: v["tier"] for k, v in res["checks"].items()}
        assert tiers["profile"] == "trading" and tiers["expiry_list"] == "data"
        assert res["profile"]["data_plan"] == "Deactive"
        assert any("NOT active" in n for n in res["notes"])


def _index_contract():
    from onelot.broker.base import Contract
    return Contract(security_id="25", exchange_segment="IDX_I", symbol="BANKNIFTY", underlying="BANKNIFTY",
                    instrument_type="INDEX")


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
        assert s["holdings_count"] == 1 and s["holdings_invested"] == 780.0
        assert s["ledger_running_balance"] == 99976.4
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
                return httpx.Response(200, json={"errorCode": "DH-905", "errorMessage": "bad dates"})
            return good_handler(req)

        svc = AccountService(engine, adapter=make_adapter(h))
        res = svc.refresh()
        assert res["ledger"]["ok"] is False and res["funds"]["ok"] is True
        snap = svc.snapshot()
        assert snap["panels"]["ledger"]["error"].startswith("BrokerError")
        assert snap["panels"]["funds"]["data"]["availabelBalance"] == 98765.5

    def test_summary_tolerates_strings_and_missing_fields(self):
        panels = {"funds": {"data": {"availabelBalance": "12.5"}}, "positions": {"data": [{"netQty": "0"}, {"netQty": "-35", "unrealizedProfit": "-10"}]},
                  "holdings": {"data": []}, "profile": {"data": {}}, "ledger": {"data": "not-a-list"}}
        s = AccountService._summary(panels)
        assert s["available_balance"] == 12.5 and s["open_positions"] == 1 and s["positions_unrealized"] == -10

    def test_json_round_trip_of_cached_payloads(self, engine):
        svc = AccountService(engine, adapter=make_adapter(good_handler))
        svc.refresh()
        json.dumps(svc.snapshot())  # must be serialisable for the API
