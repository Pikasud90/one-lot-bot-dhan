"""DhanAdapter.rolling_option: Dhan's expired index options on a rolling, ATM-relative basis.

Read-only market data (a POST that only asks for bars). The HTTP layer runs on an httpx
MockTransport, so the request Dhan would see, every answer shape and every error mapping are
checked without a network.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from onelot.broker.base import BrokerError, DataEntitlementError, RateLimitError
from onelot.broker.dhan import API_BASE, DhanAdapter
from onelot.ratelimit import RateLimiter

TOKEN = "eyJ" + "x" * 40
DAY = dt.date(2026, 10, 1)
NEXT = dt.date(2026, 10, 2)

CE_LEG = {
    "timestamp": [1790000000, 1790000060],
    "open": [310.0, 311.0],
    "high": [312.0, 313.0],
    "low": [309.0, 310.5],
    "close": [311.0, 312.5],
    "iv": [12.1, 12.2],
    "volume": [100, 120],
    "oi": [5000, 5100],
    "strike": [25000.0, 25000.0],
    "spot": [25010.5, 25020.0],
}
PE_LEG = {**CE_LEG, "open": [88.0, 87.5], "strike": [25000.0, 25000.0]}
NO_DATA_806 = {"data": {"806": "Data APIs not Subscribed"}, "status": "failed"}


def make_adapter(handler) -> DhanAdapter:
    a = DhanAdapter("1100001234", TOKEN, RateLimiter())
    a._client.close()
    a._client = httpx.Client(
        base_url=API_BASE,
        transport=httpx.MockTransport(handler),
        headers={"access-token": "t", "client-id": "c"},
    )
    return a


class Recorder:
    """A MockTransport handler that answers `body` and keeps every request it saw."""

    def __init__(self, body, status: int = 200) -> None:
        self.body, self.status = body, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[-1].content)


def wrapped(ce=CE_LEG, pe=PE_LEG) -> dict:
    return {"data": {"ce": ce, "pe": pe}, "status": "success"}


class TestTheRequest:
    def test_posts_the_documented_body_to_the_rolling_option_endpoint(self):
        rec = Recorder(wrapped())
        a = make_adapter(rec)
        a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT)
        (req,) = rec.requests
        assert req.method == "POST"
        assert req.url.path == "/v2/charts/rollingoption"
        body = rec.payload
        required = body.pop("requiredData")
        assert body == {
            "exchangeSegment": "NSE_FNO",
            "interval": "1",
            "securityId": "13",
            "instrument": "OPTIDX",
            "expiryFlag": "MONTH",
            "expiryCode": 1,
            "strike": "ATM",
            "drvOptionType": "CALL",
            "fromDate": "2026-10-01",
            "toDate": "2026-10-02",
        }
        assert sorted(required) == sorted(
            ["open", "high", "low", "close", "iv", "volume", "oi", "strike", "spot"]
        )

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            (
                {"underlying_security_id": "25", "fno_segment": "NSE_FNO", "expiry_code": 2},
                {"securityId": "25", "exchangeSegment": "NSE_FNO", "expiryCode": 2},
            ),
            (
                {"underlying_security_id": 51, "fno_segment": "BSE_FNO", "expiry_code": 1},
                {"securityId": "51", "exchangeSegment": "BSE_FNO", "expiryCode": 1},
            ),
            (
                {"underlying_security_id": 13, "fno_segment": "NSE_FNO", "expiry_code": 1,
                 "strike": "ATM-10", "option_type": "PUT"},
                {"strike": "ATM-10", "drvOptionType": "PUT"},
            ),
            (
                {"underlying_security_id": 13, "fno_segment": "NSE_FNO", "expiry_code": 1,
                 "strike": "ATM+10", "expiry_flag": "WEEK", "interval_min": 5},
                {"strike": "ATM+10", "expiryFlag": "WEEK", "interval": "5"},
            ),
        ],
    )  # fmt: skip
    def test_every_argument_reaches_the_body_as_dhan_spells_it(self, kwargs, expected):
        rec = Recorder(wrapped())
        a = make_adapter(rec)
        args = {"strike": "ATM", "option_type": "CALL", "start": DAY, "end": NEXT, **kwargs}
        a.rolling_option(**args)
        body = rec.payload
        for key, value in expected.items():
            assert body[key] == value
        assert isinstance(body["securityId"], str) and isinstance(body["interval"], str)
        assert isinstance(body["expiryCode"], int)

    def test_the_token_never_travels_in_the_body(self):
        rec = Recorder(wrapped())
        make_adapter(rec).rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT)
        text = rec.requests[0].content.decode().lower()
        assert TOKEN.lower() not in text and "token" not in text and "client" not in text

    def test_it_draws_on_the_historical_rate_limit_bucket(self):
        a = make_adapter(Recorder(wrapped()))
        seen: list[str] = []
        real = a.limiter.acquire

        def spy(endpoint, timeout=30.0):
            seen.append(endpoint)
            return real(endpoint, timeout)

        a.limiter.acquire = spy  # type: ignore[method-assign]
        a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT)
        assert seen == ["historical"]

    def test_it_never_reaches_an_order_endpoint(self):
        rec = Recorder(wrapped())
        make_adapter(rec).rolling_option(13, "NSE_FNO", 1, "ATM", "PUT", DAY, NEXT)
        assert {r.method for r in rec.requests} == {"POST"}
        assert all("order" not in r.url.path.lower() for r in rec.requests)


class TestTheAnswer:
    def test_a_call_gets_the_ce_leg_and_a_put_the_pe_leg(self):
        a = make_adapter(Recorder(wrapped()))
        assert a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT) == CE_LEG
        assert a.rolling_option(13, "NSE_FNO", 1, "ATM", "PUT", DAY, NEXT) == PE_LEG

    def test_the_two_legs_are_never_mixed_up(self):
        only_pe = make_adapter(Recorder({"data": {"pe": PE_LEG}}))
        assert only_pe.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT) == {}
        only_ce = make_adapter(Recorder({"data": {"ce": CE_LEG}}))
        assert only_ce.rolling_option(13, "NSE_FNO", 1, "ATM", "PUT", DAY, NEXT) == {}

    def test_a_body_without_the_data_wrapper_is_read_as_it_comes(self):
        a = make_adapter(Recorder({"ce": CE_LEG, "pe": PE_LEG}))
        assert a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT) == CE_LEG

    @pytest.mark.parametrize(
        "body",
        [
            {},
            [],
            {"data": None},
            {"data": []},
            {"data": "nothing"},
            {"data": {}},
            {"data": {"ce": None, "pe": None}},
            {"data": {"ce": [], "pe": []}},
            {"data": {"ce": "bad", "pe": 3}},
        ],
        ids=lambda b: json.dumps(b)[:30],
    )
    def test_no_bars_or_a_malformed_leg_is_an_empty_dict_never_an_error(self, body):
        a = make_adapter(Recorder(body))
        assert a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT) == {}
        assert a.rolling_option(13, "NSE_FNO", 1, "ATM", "PUT", DAY, NEXT) == {}


class TestRefusedInput:
    @pytest.mark.parametrize("bad", ["CE", "PE", "call", "put", "Call", "", "BOTH", None])
    def test_an_option_type_other_than_call_or_put_is_refused_before_any_request(self, bad):
        rec = Recorder(wrapped())
        with pytest.raises(ValueError, match="CALL or PUT"):
            make_adapter(rec).rolling_option(13, "NSE_FNO", 1, "ATM", bad, DAY, NEXT)
        assert rec.requests == []


class TestErrors:
    def test_a_free_plan_token_is_an_entitlement_problem_not_a_token_problem(self):
        a = make_adapter(Recorder(NO_DATA_806, status=401))
        with pytest.raises(DataEntitlementError):
            a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT)

    def test_dhans_refusal_of_a_long_window_is_a_broker_error_and_is_not_retried(self):
        body = {
            "errorType": "Input_Exception",
            "errorCode": "DH-905",
            "errorMessage": "Date range too long",
        }
        rec = Recorder(body, status=200)
        a = make_adapter(rec)
        with pytest.raises(BrokerError, match="DH-905"):
            a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, dt.date(2026, 11, 1))
        assert len(rec.requests) == 1

    def test_a_missing_expiry_code_message_surfaces(self):
        body = {
            "errorType": "Input_Exception",
            "errorCode": "DH-905",
            "errorMessage": "expiryCode is required",
        }
        a = make_adapter(Recorder(body, status=400))
        with pytest.raises(BrokerError, match="expiryCode is required"):
            a.rolling_option(13, "NSE_FNO", 0, "ATM", "CALL", DAY, NEXT)

    def test_rate_limited_calls_retry_then_raise(self):
        rec = Recorder({"errorCode": "DH-904", "errorMessage": "rate"}, status=429)
        a = make_adapter(rec)
        a.limiter.note_rate_limited = lambda *_a, **_k: 0.0  # no real sleeping in tests
        with pytest.raises(RateLimitError):
            a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT)
        assert len(rec.requests) == 3

    def test_a_network_failure_is_a_broker_error_that_never_leaks_the_token(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach host with {TOKEN}")

        a = make_adapter(boom)
        with pytest.raises(BrokerError) as ei:
            a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT)
        assert "/charts/rollingoption" in str(ei.value)
        assert TOKEN not in str(ei.value)

    def test_a_body_that_is_not_json_is_a_broker_error(self):
        a = make_adapter(lambda r: httpx.Response(200, text="<html>gateway</html>"))
        with pytest.raises(BrokerError, match="non-JSON"):
            a.rolling_option(13, "NSE_FNO", 1, "ATM", "CALL", DAY, NEXT)
