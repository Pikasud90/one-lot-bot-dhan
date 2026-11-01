"""US-004 (ADR-002 amendment 9): the Engine's quiet-hours rule for Telegram.

`Engine._in_session` is what TelegramChannel asks at send() time. Market hours on a trading
day are "in session"; a weekend, a listed holiday and the hours outside 09:15-15:30 IST are
not; the trade date is the IST date even when the moment arrives in another zone.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from onelot.clock import IST, SessionClock, TradingCalendar
from onelot.config import SessionCfg
from onelot.engine import Engine

HOLIDAY = dt.date(2026, 11, 10)  # a Tuesday, listed as a holiday below
UTC = dt.timezone.utc


def in_session(moment: dt.datetime) -> bool:
    calendar = TradingCalendar({HOLIDAY: "test holiday"}, verified=True)
    fake_engine = SimpleNamespace(session_clock=SessionClock(SessionCfg(), calendar))
    return Engine._in_session(fake_engine, moment)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "moment, expected",
    [
        (dt.datetime(2026, 11, 2, 10, 0, tzinfo=IST), True),  # Monday mid-morning
        (dt.datetime(2026, 11, 2, 9, 15, tzinfo=IST), True),  # the open itself
        (dt.datetime(2026, 11, 2, 15, 30, tzinfo=IST), True),  # the close itself
        (dt.datetime(2026, 11, 2, 9, 14, 59, tzinfo=IST), False),
        (dt.datetime(2026, 11, 2, 15, 30, 1, tzinfo=IST), False),
        (dt.datetime(2026, 11, 1, 11, 0, tzinfo=IST), False),  # Sunday
        (dt.datetime(2026, 11, 10, 11, 0, tzinfo=IST), False),  # the holiday
        (dt.datetime(2026, 11, 2, 4, 30, tzinfo=UTC), True),  # 10:00 IST, given in UTC
        (dt.datetime(2026, 11, 1, 19, 0, tzinfo=UTC), False),  # 00:30 IST Monday
    ],
)
def test_in_session_is_market_hours_on_an_ist_trading_day(moment, expected):
    assert in_session(moment) is expected
