"""Deterministic synthetic market and mock broker.

This is not a toy. It is the reason the system is falsifiable (requirement R9,
the single biggest gap in v1): it drives the identical alpha and guardian code
paths that run live, with no credentials, no network, and byte-identical
results for a given seed. Every integration test, the replay harness and the
backtest all run on it.

It also injects the failures that matter — missing greeks, stale chains,
rate limits, expired tokens, dead sockets — so the degradation paths are
exercised rather than assumed.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from dataclasses import dataclass, field

import numpy as np

from ..analytics.blackscholes import greeks as bs_greeks
from ..analytics.blackscholes import price as bs_price
from ..analytics.blackscholes import year_fraction
from ..clock import IST
from .base import (
    Bar,
    ChainRow,
    ChainSnapshot,
    Contract,
    QuoteSnapshot,
    RateLimitError,
    TickCallback,
    TokenExpiredError,
)

log = logging.getLogger(__name__)

MINUTES_PER_SESSION = 375          # 09:15 -> 15:30
SESSION_OPEN = dt.time(9, 15)


@dataclass
class SyntheticConfig:
    seed: int = 7
    sessions: int = 90
    start_price: float = 54_000.0
    daily_vol: float = 0.011           # ~1.1% daily sigma
    drift_per_session: float = 0.0
    futures_basis_bps: float = 8.0
    base_iv: float = 0.14
    iv_daily_vol: float = 0.05
    iv_mean_reversion: float = 0.12   # OU pull toward base_iv per session
    smile_curvature: float = 0.55
    strike_step: float = 100.0
    strikes_each_side: int = 22
    lot_size: int = 35
    # Per-SESSION log drift applied during a trending stretch. 0.004 ~ 0.4%/day.
    # Values above ~0.05 compound into absurd paths over a long history; the
    # sanity guard in SyntheticMarket._build will reject them outright.
    trend_strength: float = 0.0
    end_date: dt.date | None = None   # None => today (see _build)


class SyntheticMarket:
    """Generates a reproducible multi-session Bank Nifty tape.

    Guards its own output: a generator that silently produces a negative or
    absurd price path would make the whole strategy look broken while the
    strategy is fine. That failure mode cost real debugging time, so it now
    raises instead.
    """

    MIN_SANE_PRICE = 1_000.0
    MAX_SANE_PRICE = 1_000_000.0

    def __init__(self, cfg: SyntheticConfig | None = None):
        self.cfg = cfg or SyntheticConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        self.session_dates: list[dt.date] = []
        self.index_minutes: dict[dt.date, np.ndarray] = {}
        self.fut_minutes: dict[dt.date, np.ndarray] = {}
        self.fut_volume: dict[dt.date, np.ndarray] = {}
        self.session_iv: dict[dt.date, float] = {}
        self._build()

    # ── tape construction ─────────────────────────────────────────────────
    def _build(self) -> None:
        c = self.cfg
        end = c.end_date
        if end is None:
            # Default to today so the tape lines up with the wall clock in
            # realtime mode; roll back to the last weekday if today is a weekend.
            end = dt.datetime.now(IST).date()
            while end.weekday() > 4:
                end -= dt.timedelta(days=1)
        d = end
        dates: list[dt.date] = []
        while len(dates) < c.sessions:
            if d.weekday() < 5:
                dates.append(d)
            d -= dt.timedelta(days=1)
        self.session_dates = sorted(dates)

        px = c.start_price
        sigma_min = c.daily_vol / math.sqrt(MINUTES_PER_SESSION)
        regime_drift = 0.0

        for i, day in enumerate(self.session_dates):
            if c.trend_strength > 0 and i % 15 == 0:
                regime_drift = self.rng.choice([-1.0, 0.0, 1.0]) * c.trend_strength
            gap = self.rng.normal(0.0, c.daily_vol * 0.35)
            px *= math.exp(gap)

            mu = (c.drift_per_session + regime_drift) / MINUTES_PER_SESSION
            steps = self.rng.normal(mu, sigma_min, MINUTES_PER_SESSION)
            path = px * np.exp(np.cumsum(steps))
            self.index_minutes[day] = np.concatenate([[px], path])
            px = float(path[-1])
            if not (self.MIN_SANE_PRICE < px < self.MAX_SANE_PRICE):
                raise ValueError(
                    f"SyntheticMarket generated an implausible price ({px:,.2f}) on "
                    f"{day}. Check SyntheticConfig: drift_per_session="
                    f"{c.drift_per_session}, trend_strength={c.trend_strength} are "
                    "PER-SESSION LOG returns — 0.9 means a 146% daily move, which "
                    "compounds to zero or infinity over a long history."
                )

            basis = 1.0 + c.futures_basis_bps / 10_000.0
            fut = self.index_minutes[day] * basis
            fut = fut + self.rng.normal(0.0, 1.5, len(fut))
            self.fut_minutes[day] = fut

            # Futures volume: real, U-shaped across the session. THE INDEX HAS NONE.
            t = np.linspace(0, 1, len(fut))
            shape = 1.6 - 2.4 * t + 2.6 * t ** 2
            self.fut_volume[day] = np.maximum(
                200.0, 9_000.0 * shape * self.rng.uniform(0.7, 1.3, len(fut)))

            # Implied volatility is strongly MEAN-REVERTING in reality. A pure
            # random walk (the obvious first implementation) spends long
            # stretches pinned at an extreme, which makes IV percentile sit at
            # 0 or 100 for weeks and gives a false read on how often the IVP
            # gate actually blocks. Ornstein-Uhlenbeck in log space instead.
            prev = self.session_iv.get(self.session_dates[i - 1], c.base_iv) if i else c.base_iv
            log_prev, log_mean = math.log(prev), math.log(c.base_iv)
            log_next = (log_prev + c.iv_mean_reversion * (log_mean - log_prev)
                        + self.rng.normal(0.0, c.iv_daily_vol))
            self.session_iv[day] = float(np.clip(math.exp(log_next), 0.07, 0.45))

    # ── access ────────────────────────────────────────────────────────────
    def _minute_index(self, ts: dt.datetime) -> int:
        open_at = dt.datetime.combine(ts.date(), SESSION_OPEN, tzinfo=IST)
        m = int((ts - open_at).total_seconds() // 60)
        return int(np.clip(m, 0, MINUTES_PER_SESSION))

    def has_session(self, d: dt.date) -> bool:
        return d in self.index_minutes

    def index_price(self, ts: dt.datetime) -> float:
        day = ts.date()
        if day not in self.index_minutes:
            day = max((d for d in self.session_dates if d <= day), default=self.session_dates[-1])
            return float(self.index_minutes[day][-1])
        return float(self.index_minutes[day][self._minute_index(ts)])

    def futures_price(self, ts: dt.datetime) -> float:
        day = ts.date()
        if day not in self.fut_minutes:
            day = max((d for d in self.session_dates if d <= day), default=self.session_dates[-1])
            return float(self.fut_minutes[day][-1])
        return float(self.fut_minutes[day][self._minute_index(ts)])

    def atm_iv(self, ts: dt.datetime) -> float:
        day = ts.date()
        if day not in self.session_iv:
            day = max((d for d in self.session_dates if d <= day), default=self.session_dates[-1])
        return self.session_iv[day]

    def bars(self, day: dt.date, interval_min: int, series: str = "index") -> list[Bar]:
        """Aggregate the minute tape into bars. Index bars carry volume=None."""
        if day not in self.index_minutes:
            return []
        prices = self.index_minutes[day] if series == "index" else self.fut_minutes[day]
        vols = None if series == "index" else self.fut_volume[day]
        open_at = dt.datetime.combine(day, SESSION_OPEN, tzinfo=IST)
        out: list[Bar] = []
        for start in range(0, MINUTES_PER_SESSION, interval_min):
            seg = prices[start:start + interval_min + 1]
            if len(seg) < 2:
                continue
            v = float(np.sum(vols[start:start + interval_min])) if vols is not None else None
            out.append(Bar(
                ts=open_at + dt.timedelta(minutes=start),
                open=float(seg[0]), high=float(np.max(seg)),
                low=float(np.min(seg)), close=float(seg[-1]), volume=v,
            ))
        return out

    def daily_bars(self, series: str = "index") -> list[Bar]:
        out = []
        for day in self.session_dates:
            p = self.index_minutes[day] if series == "index" else self.fut_minutes[day]
            v = float(np.sum(self.fut_volume[day])) if series != "index" else None
            out.append(Bar(
                ts=dt.datetime.combine(day, SESSION_OPEN, tzinfo=IST),
                open=float(p[0]), high=float(np.max(p)), low=float(np.min(p)),
                close=float(p[-1]), volume=v))
        return out

    # ── option chain ──────────────────────────────────────────────────────
    def _iv_for_strike(self, strike: float, fut: float, base_iv: float) -> float:
        """A simple, realistic smile: IV rises as you move away from the money."""
        m = math.log(strike / fut) if fut > 0 else 0.0
        return float(np.clip(base_iv * (1.0 + self.cfg.smile_curvature * m * m * 40.0),
                             0.05, 0.90))

    def _spread_for(self, price: float, strike: float, fut: float) -> float:
        """Spread model: wider in rupees for expensive ITM strikes, wider in
        percentage terms for cheap far-OTM ones. A flat Rs 1.50 cap (v1 defect
        B1) rejects essentially every ITM monthly contract."""
        dist = abs(strike - fut) / max(fut, 1.0)
        base = 0.25 + 0.006 * price + 60.0 * dist * max(price, 1.0) * 0.002
        return max(0.05, round(base / 0.05) * 0.05)

    def option_chain(self, ts: dt.datetime, expiry: dt.datetime,
                     with_greeks: bool = True) -> tuple[float, list[ChainRow]]:
        c = self.cfg
        fut = self.futures_price(ts)
        base_iv = self.atm_iv(ts)
        t = year_fraction(ts, expiry)
        atm = round(fut / c.strike_step) * c.strike_step
        rows: list[ChainRow] = []

        for i in range(-c.strikes_each_side, c.strikes_each_side + 1):
            k = atm + i * c.strike_step
            if k <= 0:
                continue
            iv = self._iv_for_strike(k, fut, base_iv)
            dist = abs(k - fut) / max(fut, 1.0)
            liq = math.exp(-((dist * 45.0) ** 2))
            for opt in ("CE", "PE"):
                fair = bs_price(fut, k, t, iv, 0.0, opt)
                if fair < 0.05:
                    fair = 0.05
                spread = self._spread_for(fair, k, fut)
                bid = max(0.05, round((fair - spread / 2) / 0.05) * 0.05)
                ask = round((bid + spread) / 0.05) * 0.05
                g = bs_greeks(fut, k, t, iv, 0.0, opt) if with_greeks else None
                rows.append(ChainRow(
                    strike=float(k), opt_type=opt,
                    security_id=f"MOCK{int(k)}{opt}",
                    ltp=round((bid + ask) / 2, 2), bid=round(bid, 2), ask=round(ask, 2),
                    bid_qty=int(max(1, 40 * liq) * c.lot_size),
                    ask_qty=int(max(1, 40 * liq) * c.lot_size),
                    volume=float(int(90_000 * liq + 120)),
                    oi=float(int(700_000 * liq + 6_000)),
                    iv=round(iv, 4),
                    delta=round(g.delta, 4) if g else None,
                    gamma=round(g.gamma, 6) if g else None,
                    theta=round(g.theta, 4) if g else None,
                    vega=round(g.vega, 4) if g else None,
                    greek_source="broker" if with_greeks else "absent",
                ))
        return fut, rows


@dataclass
class Faults:
    """Failure injection. Every one of these is a real production condition."""

    omit_greeks: bool = False
    corrupt_delta: float | None = None      # additive error on broker delta
    chain_staleness_sec: float = 0.0
    rate_limit_every_n: int = 0
    token_expired: bool = False
    socket_dead: bool = False
    quote_failures: int = 0
    zero_bid_strikes: set = field(default_factory=set)


class MockBroker:
    """BrokerAdapter over a SyntheticMarket. No network, no credentials."""

    name = "mock"

    def __init__(self, market: SyntheticMarket | None = None,
                 now: dt.datetime | None = None,
                 expiry: dt.date | None = None,
                 faults: Faults | None = None,
                 underlying: str = "BANKNIFTY",
                 realtime: bool = False,
                 tick_interval_sec: float = 1.0):
        self.market = market or SyntheticMarket()
        self.underlying = underlying
        self._now = now or dt.datetime.combine(
            self.market.session_dates[-1], dt.time(10, 0), tzinfo=IST)
        self.expiry_date = expiry or self._default_expiry()
        self.faults = faults or Faults()
        self._call_counts: dict[str, int] = {}
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        self._subscribed: list[Contract] = []
        self._on_tick: TickCallback | None = None
        self._stream_started = False
        # Realtime mode makes `--mock` behave like a live feed: the clock
        # follows the wall clock and ticks are pushed on a thread. Replay mode
        # (the default) stays fully deterministic and driven by pump().
        self.realtime = realtime
        self.tick_interval_sec = tick_interval_sec

    def _default_expiry(self) -> dt.date:
        """Last weekday of the current month — the mock's own convention.
        The real adapter never computes this; it reads the broker's expiry list."""
        d = self.market.session_dates[-1]
        nxt = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        last = nxt - dt.timedelta(days=1)
        while last.weekday() > 4:
            last -= dt.timedelta(days=1)
        return last

    @property
    def expiry_dt(self) -> dt.datetime:
        return dt.datetime.combine(self.expiry_date, dt.time(15, 30), tzinfo=IST)

    def set_now(self, ts: dt.datetime) -> None:
        """Replay clock control."""
        self._now = ts

    def now(self) -> dt.datetime:
        if self.realtime:
            return self._wall_clock_now()
        return self._now

    def _wall_clock_now(self) -> dt.datetime:
        """Map the wall clock onto the synthetic tape.

        Outside market hours we clamp to the session boundary so prices stay
        defined rather than becoming NaN — the engine's own session gates are
        what decide whether trading is allowed, not the data layer.
        """
        real = dt.datetime.now(IST)
        day = real.date()
        if day not in self.market.index_minutes:
            day = self.market.session_dates[-1]
        open_at = dt.datetime.combine(day, SESSION_OPEN, tzinfo=IST)
        close_at = open_at + dt.timedelta(minutes=MINUTES_PER_SESSION)
        candidate = dt.datetime.combine(day, real.timetz())
        if candidate < open_at:
            return open_at
        if candidate > close_at:
            return close_at
        return candidate

    def _count(self, key: str) -> int:
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        return self._call_counts[key]

    def _guard(self, key: str) -> None:
        if self.faults.token_expired:
            raise TokenExpiredError("mock: access token expired")
        n = self._count(key)
        if self.faults.rate_limit_every_n and n % self.faults.rate_limit_every_n == 0:
            raise RateLimitError(f"mock: RL001 on {key} (call #{n})")

    # ── BrokerAdapter ─────────────────────────────────────────────────────
    def preflight(self) -> dict:
        return {"ok": not self.faults.token_expired, "broker": "mock",
                "data_api": True, "notes": ["synthetic market — no credentials required"]}

    def expiry_list(self, underlying: str) -> list[str]:
        self._guard("reference")
        base = self.expiry_date
        out = [base.isoformat()]
        for i in (1, 2):
            m = base.month + i
            y = base.year + (m - 1) // 12
            m = (m - 1) % 12 + 1
            nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
            last = nxt - dt.timedelta(days=1)
            while last.weekday() > 4:
                last -= dt.timedelta(days=1)
            out.append(last.isoformat())
        return out

    def instruments(self, underlying: str) -> list[Contract]:
        self._guard("reference")
        _, rows = self.market.option_chain(self._now, self.expiry_dt, with_greeks=False)
        out = [
            Contract(security_id="MOCKIDX", exchange_segment="IDX_I",
                     symbol=underlying, underlying=underlying, instrument_type="INDEX"),
            Contract(security_id="MOCKFUT", exchange_segment="NSE_FNO",
                     symbol=f"{underlying}FUT", underlying=underlying,
                     instrument_type="FUTIDX", expiry=self.expiry_date.isoformat(),
                     lot_size=self.market.cfg.lot_size),
        ]
        for r in rows:
            out.append(Contract(
                security_id=r.security_id or "", exchange_segment="NSE_FNO",
                symbol=f"{underlying}{r.strike:g}{r.opt_type}", underlying=underlying,
                instrument_type="OPTIDX", expiry=self.expiry_date.isoformat(),
                strike=r.strike, opt_type=r.opt_type,
                lot_size=self.market.cfg.lot_size, tick_size=0.05))
        return out

    def option_chain(self, underlying: str, expiry: str) -> ChainSnapshot:
        self._guard("option_chain")
        f = self.faults
        ts_now = self.now()
        _, rows = self.market.option_chain(
            ts_now, self.expiry_dt, with_greeks=not f.omit_greeks)
        if f.corrupt_delta is not None:
            for r in rows:
                if r.delta is not None:
                    r.delta = round(r.delta + f.corrupt_delta, 4)
        if f.zero_bid_strikes:
            for r in rows:
                if r.strike in f.zero_bid_strikes:
                    r.bid = 0.0
        # In realtime mode the snapshot must carry a WALL-CLOCK timestamp, or
        # the engine's freshness check sees it as a day stale.
        stamp = (dt.datetime.now(IST) if self.realtime else ts_now)
        stamp -= dt.timedelta(seconds=f.chain_staleness_sec)
        return ChainSnapshot(ts=stamp, underlying=underlying, expiry=expiry,
                             underlying_price=self.market.futures_price(ts_now),
                             rows=rows)

    def quote(self, contracts: list[Contract]) -> dict[str, QuoteSnapshot]:
        self._guard("quote")
        if self.faults.quote_failures > 0:
            self.faults.quote_failures -= 1
            raise RateLimitError("mock: quote endpoint throttled")
        ts_now = self.now()
        stamp = dt.datetime.now(IST) if self.realtime else ts_now
        _, rows = self.market.option_chain(ts_now, self.expiry_dt)
        by_id = {r.security_id: r for r in rows}
        out: dict[str, QuoteSnapshot] = {}
        for c in contracts:
            if c.instrument_type == "INDEX":
                p = self.market.index_price(ts_now)
                out[c.security_id] = QuoteSnapshot(
                    c.security_id, stamp, ltp=p, bid=None, ask=None,
                    volume=None, source="rest")   # index: no book, NO VOLUME
            elif c.instrument_type == "FUTIDX":
                p = self.market.futures_price(ts_now)
                out[c.security_id] = QuoteSnapshot(
                    c.security_id, stamp, ltp=p, bid=round(p - 1.5, 2),
                    ask=round(p + 1.5, 2),
                    volume=float(self.market.fut_volume[ts_now.date()][
                        self.market._minute_index(ts_now) - 1])
                    if self.market.has_session(ts_now.date()) else 0.0,
                    source="rest")
            else:
                r = by_id.get(c.security_id)
                if r:
                    out[c.security_id] = QuoteSnapshot(
                        c.security_id, stamp, ltp=r.ltp, bid=r.bid, ask=r.ask,
                        bid_qty=r.bid_qty, ask_qty=r.ask_qty, volume=r.volume,
                        oi=r.oi, source="rest")
        return out

    def historical_daily(self, contract: Contract, start: dt.date,
                         end: dt.date) -> list[Bar]:
        self._guard("historical")
        series = "index" if contract.instrument_type == "INDEX" else "futures"
        return [b for b in self.market.daily_bars(series) if start <= b.ts.date() <= end]

    def historical_intraday(self, contract: Contract, interval_min: int,
                            start: dt.datetime, end: dt.datetime) -> list[Bar]:
        self._guard("historical")
        series = "index" if contract.instrument_type == "INDEX" else "futures"
        out: list[Bar] = []
        d = start.date()
        while d <= end.date():
            out.extend(b for b in self.market.bars(d, interval_min, series)
                       if start <= b.ts <= end)
            d += dt.timedelta(days=1)
        return out

    # ── streaming ─────────────────────────────────────────────────────────
    def start_stream(self, contracts: list[Contract], on_tick: TickCallback) -> None:
        self._subscribed = list(contracts)
        self._on_tick = on_tick
        self._stream_started = True
        self._stream_stop.clear()
        if self.realtime:
            self._stream_thread = threading.Thread(
                target=self._realtime_loop, name="mock-feed", daemon=True)
            self._stream_thread.start()

    def _realtime_loop(self) -> None:
        while not self._stream_stop.is_set():
            try:
                self.pump()
            except Exception:
                log.debug("mock realtime pump failed", exc_info=True)
            self._stream_stop.wait(self.tick_interval_sec)

    def update_subscription(self, contracts: list[Contract]) -> None:
        known = {c.security_id for c in self._subscribed}
        self._subscribed.extend(c for c in contracts if c.security_id not in known)

    def stop_stream(self) -> None:
        self._stream_stop.set()
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=3.0)
            self._stream_thread = None
        self._stream_started = False

    def stream_alive(self) -> bool:
        return self._stream_started and not self.faults.socket_dead

    def pump(self, ts: dt.datetime | None = None) -> int:
        """Deterministically emit one tick per subscribed contract.

        Replay drives the stream synchronously instead of racing a thread —
        which is what makes a backtest byte-reproducible.
        """
        if ts:
            self.set_now(ts)
        if not self._on_tick or not self.stream_alive():
            return 0
        sent = 0
        for _sid, q in self.quote(self._subscribed).items():
            q.source = "ws"
            self._on_tick(q)
            sent += 1
        return sent
