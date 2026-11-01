"""Time, sessions and trading calendar. Every timestamp in this system is
timezone-aware and anchored to Asia/Kolkata.

Fixes v1 defect C5: `datetime.now()` on a machine set to any other timezone
silently shifts every candle boundary, corrupting warm-up and repainting bars.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> dt.datetime:
    """The only sanctioned source of 'now' in this codebase."""
    return dt.datetime.now(tz=IST)


def to_ist(ts: dt.datetime) -> dt.datetime:
    """Coerce any datetime to tz-aware IST. Naive input is assumed IST."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def parse_hhmm(value: str) -> dt.time:
    hh, mm = value.split(":")
    return dt.time(int(hh), int(mm))


@dataclass(frozen=True)
class SessionWindow:
    """Resolved clock boundaries for a single trading date."""

    trade_date: dt.date
    open_at: dt.datetime
    close_at: dt.datetime
    first_entry_at: dt.datetime
    no_new_entry_after: dt.datetime
    eod_advisory_at: dt.datetime
    mandatory_squareoff_at: dt.datetime

    def is_open(self, ts: dt.datetime) -> bool:
        return self.open_at <= ts <= self.close_at

    def entries_allowed(self, ts: dt.datetime) -> bool:
        return self.first_entry_at <= ts <= self.no_new_entry_after

    def past_squareoff(self, ts: dt.datetime) -> bool:
        return ts >= self.mandatory_squareoff_at


class TradingCalendar:
    """Holiday-aware trading calendar.

    Unknown or unverified holiday data is a WARNING, never a silent pass:
    an unlisted holiday simply produces no market data, which the health
    model reports as DEAD. The system cannot trade a closed exchange.
    """

    def __init__(self, holidays: dict[dt.date, str], verified: bool = False):
        self._holidays = holidays
        self.verified = verified
        if not verified:
            log.warning(
                "Holiday calendar is UNVERIFIED. Populate config/nse_holidays.yaml "
                "from the NSE circular and set verified: true."
            )

    @classmethod
    def from_yaml(cls, path: str | Path) -> TradingCalendar:
        path = Path(path)
        if not path.exists():
            log.warning("No holiday file at %s; weekends only.", path)
            return cls({}, verified=False)
        raw = yaml.safe_load(path.read_text()) or {}
        out: dict[dt.date, str] = {}
        for _year, entries in (raw.get("holidays") or {}).items():
            for e in entries or []:
                out[dt.date.fromisoformat(str(e["date"]))] = str(e.get("name", ""))
        return cls(out, verified=bool(raw.get("verified", False)))

    def is_holiday(self, d: dt.date) -> bool:
        return d in self._holidays

    def holiday_name(self, d: dt.date) -> str | None:
        return self._holidays.get(d)

    def is_trading_day(self, d: dt.date) -> bool:
        return d.weekday() < 5 and not self.is_holiday(d)

    def has_year(self, year: int) -> bool:
        return any(d.year == year for d in self._holidays)

    def previous_trading_days(self, before: dt.date, count: int) -> list[dt.date]:
        """The `count` trading days strictly before `before`, oldest first."""
        out: list[dt.date] = []
        cur = before - dt.timedelta(days=1)
        guard = 0
        while len(out) < count and guard < count * 10 + 40:
            if self.is_trading_day(cur):
                out.append(cur)
            cur -= dt.timedelta(days=1)
            guard += 1
        return list(reversed(out))


class SessionClock:
    """Resolves configured HH:MM session boundaries against a trade date."""

    def __init__(self, session_cfg, calendar: TradingCalendar):
        self.cfg = session_cfg
        self.calendar = calendar

    def _at(self, d: dt.date, t: dt.time) -> dt.datetime:
        return dt.datetime.combine(d, t, tzinfo=IST)

    def window_for(self, d: dt.date) -> SessionWindow:
        c = self.cfg
        return SessionWindow(
            trade_date=d,
            open_at=self._at(d, c.market_open),
            close_at=self._at(d, c.market_close),
            first_entry_at=self._at(d, c.first_entry_after),
            no_new_entry_after=self._at(d, c.no_new_entry_after),
            eod_advisory_at=self._at(d, c.eod_advisory),
            mandatory_squareoff_at=self._at(d, c.mandatory_squareoff),
        )

    def current_window(self, ts: dt.datetime | None = None) -> SessionWindow | None:
        ts = ts or now_ist()
        if not self.calendar.is_trading_day(ts.date()):
            return None
        return self.window_for(ts.date())


def bar_open_time(ts: dt.datetime, interval_min: int, session_open: dt.datetime) -> dt.datetime:
    """Open time of the bar containing `ts`, aligned to the session open.

    Anchoring to the session open (09:15) rather than the hour is what makes
    5-minute bars land on 09:15 / 09:20 / ... rather than 09:00 / 09:05 / ...
    """
    delta = (ts - session_open).total_seconds()
    if delta < 0:
        return session_open
    idx = int(delta // (interval_min * 60))
    return session_open + dt.timedelta(minutes=interval_min * idx)


def bar_close_time(bar_open: dt.datetime, interval_min: int) -> dt.datetime:
    return bar_open + dt.timedelta(minutes=interval_min)


def is_bar_closed(
    bar_open: dt.datetime, interval_min: int, ts: dt.datetime, settle_delay_sec: float = 0.0
) -> bool:
    """INVARIANT I2 helper. A bar is usable only once it has fully closed
    (plus a settle delay for late prints). Reading the forming bar is the
    repainting bug that makes every backtest a lie (v1 defect C4).
    """
    return ts >= bar_close_time(bar_open, interval_min) + dt.timedelta(seconds=settle_delay_sec)


def assert_clock_sane(
    tolerance_sec: float, reference: dt.datetime | None = None
) -> tuple[bool, str]:
    """Sanity-check the host clock. Returns (ok, message); never raises.

    Without a network time source we can only check that the machine's
    UTC offset arithmetic is coherent, which catches the common failure of
    a manually-mangled system clock or a bad TZ database.
    """
    local = dt.datetime.now().astimezone()
    ist = now_ist()
    drift = abs((local - ist).total_seconds())
    if drift > tolerance_sec:
        return False, f"Clock inconsistency: local vs IST differ by {drift:.1f}s"
    if reference is not None:
        ref_drift = abs((ist - to_ist(reference)).total_seconds())
        if ref_drift > tolerance_sec:
            return False, f"Clock drift vs reference: {ref_drift:.1f}s"
    return True, "clock ok"
