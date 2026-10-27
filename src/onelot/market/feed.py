"""Market feed orchestration: warm-up, streaming, failover, health.

Owns the three data sources the strategy needs and keeps them coherent:

    INDEX    — signal generation (EMA/RSI/ATR). Has NO volume, ever.
    FUTURES  — VWAP, volume divergence, and the forward used for pricing
               (v1 defect B3: the index cannot supply any of these).
    OPTIONS  — the selected contract's live quote, for the Guardian.

Failover follows the corrected rule (v1 defect D4): REST takes over when the
SOCKET is dead, never merely because ticks have gone quiet.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass

from ..broker.base import Bar, Contract, QuoteSnapshot, RateLimitError, TokenExpiredError
from ..clock import IST, now_ist
from ..ratelimit import RateLimiter
from .candles import CandleBook, CandleSeries
from .chain import ChainCache
from .health import HealthMonitor, HealthReport

log = logging.getLogger(__name__)

INDEX_KEY = "INDEX"
FUTURES_KEY = "FUTURES"


@dataclass
class FeedContracts:
    index: Contract
    futures: Contract | None = None
    options: dict[str, Contract] = None

    def __post_init__(self):
        if self.options is None:
            self.options = {}

    def all(self) -> list[Contract]:
        out = [self.index]
        if self.futures:
            out.append(self.futures)
        out.extend(self.options.values())
        return out


class MarketFeed:
    def __init__(self, broker, contracts: FeedContracts, limiter: RateLimiter,
                 health: HealthMonitor, session_open: dt.datetime,
                 interval_min: int = 5, settle_delay_sec: float = 2.0,
                 rest_failover_interval_sec: float = 1.0,
                 on_bar_closed=None):
        self.broker = broker
        self.contracts = contracts
        self.limiter = limiter
        self.health = health
        self.interval_min = interval_min
        self.settle_delay_sec = settle_delay_sec
        self.rest_failover_interval_sec = rest_failover_interval_sec
        self.on_bar_closed = on_bar_closed

        self.book = CandleBook(session_open=session_open, interval_min=interval_min)
        # has_volume is a deliberate, explicit property of each series.
        self.index_series: CandleSeries = self.book.ensure(INDEX_KEY, has_volume=False)
        self.futures_series: CandleSeries = self.book.ensure(FUTURES_KEY, has_volume=True)

        self.latest_quotes: dict[str, QuoteSnapshot] = {}
        self.chain: ChainCache | None = None

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._failover_thread: threading.Thread | None = None
        self._id_to_key: dict[str, str] = {}
        self._rebuild_id_map()

    def _rebuild_id_map(self) -> None:
        self._id_to_key = {str(self.contracts.index.security_id): INDEX_KEY}
        if self.contracts.futures:
            self._id_to_key[str(self.contracts.futures.security_id)] = FUTURES_KEY
        for key, c in self.contracts.options.items():
            self._id_to_key[str(c.security_id)] = key

    # ── warm-up (v1 defect B4) ────────────────────────────────────────────
    def warm_up(self, sessions: int, calendar, today: dt.date) -> tuple[bool, str]:
        """Seed indicator buffers from prior sessions.

        Without this, EMA(21)/RSI(14)/ADX(14) are NaN until roughly 10:30 and
        the system sits blind through the most tradeable part of the day.
        """
        days = calendar.previous_trading_days(today, sessions)
        if not days:
            return False, "no prior trading days available for warm-up"
        start = dt.datetime.combine(days[0], dt.time(9, 15), tzinfo=IST)
        end = dt.datetime.combine(today, dt.time(15, 30), tzinfo=IST)

        seeded = {}
        for key, contract, series in (
            (INDEX_KEY, self.contracts.index, self.index_series),
            (FUTURES_KEY, self.contracts.futures, self.futures_series),
        ):
            if contract is None:
                continue
            try:
                bars = self.broker.historical_intraday(
                    contract, self.interval_min, start, end)
            except TokenExpiredError:
                raise
            except Exception as exc:
                log.warning("Warm-up fetch failed for %s: %s", key, exc)
                bars = []
            seeded[key] = series.seed_from_history(bars)

        required = self._required_bars()
        ok_idx, note_idx = self.index_series.warmup_status(required)
        ok_fut, note_fut = (self.futures_series.warmup_status(required)
                            if self.contracts.futures else (True, "no futures configured"))
        ok = ok_idx and ok_fut
        note = f"index: {note_idx}; futures: {note_fut}"
        self.health.warmup_complete = ok
        self.health.warmup_note = note if not ok else "warm-up complete"
        log.info("Warm-up seeded %s bars -> %s", seeded, note)
        return ok, note

    def _required_bars(self) -> int:
        # ADX(14) is the slowest: ~2 x period before it is defined at all.
        return 40

    # ── streaming ─────────────────────────────────────────────────────────
    def start(self) -> None:
        self._stop.clear()
        try:
            self.broker.start_stream(self.contracts.all(), self._on_tick)
            self.health.note_socket(self.broker.stream_alive(), now_ist())
        except Exception as exc:
            log.warning("Could not start market feed stream: %s", exc)
            self.health.note_socket(False, now_ist())
        self._failover_thread = threading.Thread(
            target=self._failover_loop, name="rest-failover", daemon=True)
        self._failover_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.broker.stop_stream()
        except Exception:
            pass
        if self.chain:
            self.chain.stop()
        if self._failover_thread:
            self._failover_thread.join(timeout=5.0)

    def subscribe_option(self, key: str, contract: Contract) -> None:
        """Subscribe the selected contract the moment a signal is generated."""
        with self._lock:
            self.contracts.options[key] = contract
            self._rebuild_id_map()
        try:
            self.broker.update_subscription([contract])
        except Exception as exc:
            log.warning("Live subscription update failed for %s: %s", key, exc)

    # ── tick handling ─────────────────────────────────────────────────────
    def _on_tick(self, q: QuoteSnapshot) -> None:
        key = self._id_to_key.get(str(q.security_id))
        with self._lock:
            self.latest_quotes[str(q.security_id)] = q
        self.health.note_quote(q.security_id, q.ts)
        if q.source == "ws":
            self.health.note_socket(True, q.ts)

        if key is None or q.ltp is None:
            return
        closed: list[Bar] = []
        if key == INDEX_KEY:
            # Index volume is meaningless; never feed it in.
            closed = self.index_series.on_tick(q.ts, q.ltp, None)
        elif key == FUTURES_KEY:
            closed = self.futures_series.on_tick(q.ts, q.ltp, q.volume)
        if closed and self.on_bar_closed:
            for bar in closed:
                try:
                    self.on_bar_closed(key, bar)
                except Exception:
                    log.exception("on_bar_closed handler failed for %s", key)

    def tick_time_boundaries(self, now: dt.datetime | None = None) -> list[tuple[str, Bar]]:
        """Close bars on the clock, so a quiet interval cannot stall the strategy."""
        now = now or now_ist()
        out = []
        for key, series in ((INDEX_KEY, self.index_series), (FUTURES_KEY, self.futures_series)):
            for bar in series.mark_closed_through(now, self.settle_delay_sec):
                out.append((key, bar))
                if self.on_bar_closed:
                    try:
                        self.on_bar_closed(key, bar)
                    except Exception:
                        log.exception("on_bar_closed handler failed for %s", key)
        return out

    # ── REST failover (v1 defect D4) ──────────────────────────────────────
    def _failover_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = now_ist()
                alive = False
                try:
                    alive = self.broker.stream_alive()
                except Exception:
                    alive = False
                self.health.note_socket(alive, now)

                if self.health.should_failover_to_rest(now):
                    if not self.health.using_rest_failover:
                        log.warning("WebSocket dead — engaging REST failover "
                                    "(signals are suppressed while DEGRADED)")
                    self.health.using_rest_failover = True
                    self._poll_rest_once()
                elif self.health.using_rest_failover and alive:
                    log.info("WebSocket restored — leaving REST failover")
                    self.health.using_rest_failover = False
            except Exception:
                log.exception("failover loop error")
            self._stop.wait(self.rest_failover_interval_sec)

    def _poll_rest_once(self) -> None:
        try:
            quotes = self.broker.quote(self.contracts.all())
            self.health.note_api_ok()
        except RateLimitError as exc:
            self.health.note_api_error(str(exc))
            return
        except TokenExpiredError as exc:
            self.health.note_api_error(str(exc))
            self.health.token_expires_at = now_ist()
            return
        except Exception as exc:
            self.health.note_api_error(f"{type(exc).__name__}: {exc}")
            return
        for q in quotes.values():
            q.source = "rest"
            self._on_tick(q)

    # ── accessors ─────────────────────────────────────────────────────────
    def quote_for(self, security_id: str) -> QuoteSnapshot | None:
        with self._lock:
            return self.latest_quotes.get(str(security_id))

    def quote_age_sec(self, security_id: str, now: dt.datetime) -> float | None:
        q = self.quote_for(security_id)
        return None if q is None else max(0.0, (now - q.ts).total_seconds())

    def request_quote(self, contract: Contract) -> QuoteSnapshot | None:
        """One-shot REST quote for a single contract, routed through the
        shared limiter and fed into the normal tick path. Used by the Guardian
        when the socket has gone quiet on an OPEN position's option."""
        try:
            quotes = self.broker.quote([contract])
        except (RateLimitError, TokenExpiredError) as exc:
            self.health.note_api_error(f"{type(exc).__name__}: {exc}")
            return None
        except Exception as exc:
            self.health.note_api_error(f"{type(exc).__name__}: {exc}")
            return None
        q = quotes.get(str(contract.security_id)) if quotes else None
        if q is not None:
            q.source = "rest"
            self._on_tick(q)
        return q

    def futures_price(self) -> float | None:
        if not self.contracts.futures:
            return None
        q = self.quote_for(self.contracts.futures.security_id)
        return q.ltp if q else None

    def index_price(self) -> float | None:
        q = self.quote_for(self.contracts.index.security_id)
        return q.ltp if q else None

    def health_report(self, now: dt.datetime | None = None) -> HealthReport:
        critical = [str(self.contracts.index.security_id)]
        if self.contracts.futures:
            critical.append(str(self.contracts.futures.security_id))
        return self.health.evaluate(now or now_ist(), critical)
