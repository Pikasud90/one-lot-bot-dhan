"""Option chain cache: 5-second polling, local greek validation, recording.

Keeps v1's good idea (an in-memory cache in front of the API so the strategy
sees zero-latency data) and fixes what surrounded it:

  * polling goes through the SHARED rate limiter, not a private sleep (D3)
  * greeks are cross-checked against local Black-76 and repaired when the
    broker omits them, instead of being trusted blindly (A9)
  * a stale snapshot is reported as stale rather than served as if fresh
  * chain rows with a zero bid or ask are marked unusable, not sized against
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass

from ..analytics.blackscholes import cross_check_delta, greeks_from_quote, year_fraction
from ..analytics.ivp import atm_iv_from_chain
from ..broker.base import ChainRow, ChainSnapshot, RateLimitError
from ..clock import now_ist

log = logging.getLogger(__name__)


@dataclass
class ChainStats:
    polls: int = 0
    failures: int = 0
    rate_limited: int = 0
    greeks_repaired: int = 0
    greeks_rejected: int = 0
    last_error: str | None = None


class ChainCache:
    """Thread-safe latest-chain holder plus its polling loop."""

    def __init__(
        self,
        broker,
        underlying: str,
        expiry: str,
        poll_interval_sec: float = 5.0,
        stale_after_sec: float = 15.0,
        greek_tolerance: float = 0.08,
        forward_price_fn=None,
        on_snapshot=None,
    ):
        self.broker = broker
        self.underlying = underlying
        self.expiry = expiry
        self.poll_interval_sec = poll_interval_sec
        self.stale_after_sec = stale_after_sec
        self.greek_tolerance = greek_tolerance
        self.forward_price_fn = forward_price_fn
        self.on_snapshot = on_snapshot

        self._snapshot: ChainSnapshot | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = ChainStats()

    # ── access ────────────────────────────────────────────────────────────
    @property
    def snapshot(self) -> ChainSnapshot | None:
        with self._lock:
            return self._snapshot

    def age_seconds(self, now: dt.datetime | None = None) -> float:
        snap = self.snapshot
        if snap is None:
            return float("inf")
        return snap.age_seconds(now or now_ist())

    def is_fresh(self, now: dt.datetime | None = None) -> bool:
        return self.age_seconds(now) <= self.stale_after_sec

    def atm_iv(self) -> float:
        snap = self.snapshot
        if snap is None:
            return float("nan")
        fwd = self._forward(snap)
        return atm_iv_from_chain(snap.as_dicts(), fwd)

    def _forward(self, snap: ChainSnapshot) -> float:
        if self.forward_price_fn:
            try:
                f = float(self.forward_price_fn())
                if f > 0:
                    return f
            except Exception:
                pass
        return snap.underlying_price

    # ── enrichment ────────────────────────────────────────────────────────
    def enrich(
        self, snap: ChainSnapshot, expiry_dt: dt.datetime, now: dt.datetime
    ) -> ChainSnapshot:
        """Attach locally computed greeks and validate the broker's.

        Three outcomes per row:
          broker greeks agree with local   -> greek_source='cross_checked'
          broker greeks absent             -> greek_source='local'
          broker greeks disagree materially-> greek_source='rejected', row unusable
        """
        fwd = self._forward(snap)
        t = year_fraction(now, expiry_dt)
        if fwd <= 0 or t <= 0:
            return snap

        for r in snap.rows:
            mid = None
            if r.bid and r.ask and r.ask >= r.bid > 0:
                mid = (r.bid + r.ask) / 2.0
            elif r.ltp and r.ltp > 0:
                mid = r.ltp
            if mid is None or mid <= 0:
                r.greek_source = "no_market"
                continue

            local = greeks_from_quote(mid, fwd, r.strike, t, 0.0, r.opt_type)
            ok, _why = cross_check_delta(r.delta, local, self.greek_tolerance)

            if local is None:
                r.greek_source = "unsolvable"
                continue
            if r.delta is None:
                r.delta, r.gamma = local.delta, local.gamma
                r.theta, r.vega = local.theta, local.vega
                r.iv = r.iv if r.iv else local.iv
                r.greek_source = "local"
                self.stats.greeks_repaired += 1
            elif ok:
                # Keep the broker's numbers but ensure the PE sign convention
                # is ours: negative. Some feeds report puts unsigned, which
                # would silently pass an abs() filter but corrupt the stop math.
                if r.opt_type == "PE" and r.delta > 0:
                    r.delta = -r.delta
                if r.theta is None:
                    r.theta = local.theta
                if r.vega is None:
                    r.vega = local.vega
                if not r.iv:
                    r.iv = local.iv
                r.greek_source = "cross_checked"
            else:
                r.greek_source = "rejected"
                self.stats.greeks_rejected += 1
        return snap

    # ── polling ───────────────────────────────────────────────────────────
    def poll_once(self, expiry_dt: dt.datetime, now: dt.datetime | None = None) -> bool:
        now = now or now_ist()
        try:
            snap = self.broker.option_chain(self.underlying, self.expiry)
            self.stats.polls += 1
        except RateLimitError as exc:
            self.stats.rate_limited += 1
            self.stats.last_error = str(exc)
            log.warning("Option chain rate limited: %s", exc)
            return False
        except Exception as exc:
            self.stats.failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Option chain poll failed: %s", self.stats.last_error)
            return False

        snap = self.enrich(snap, expiry_dt, now)
        with self._lock:
            self._snapshot = snap
        if self.on_snapshot:
            try:
                self.on_snapshot(snap)
            except Exception:
                log.exception("chain on_snapshot callback failed")
        return True

    def start(self, expiry_dt_fn) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.poll_once(expiry_dt_fn())
                except Exception:
                    log.exception("chain poll loop error")
                self._stop.wait(self.poll_interval_sec)

        self._thread = threading.Thread(target=loop, name="chain-poller", daemon=True)
        self._thread.start()
        log.info("Chain poller started (every %.1fs)", self.poll_interval_sec)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def usable_rows(self, opt_type: str | None = None) -> list[ChainRow]:
        """Rows safe to size against: two-sided market and trustworthy greeks."""
        snap = self.snapshot
        if snap is None:
            return []
        out = []
        for r in snap.rows:
            if opt_type and r.opt_type != opt_type:
                continue
            if r.greek_source in ("rejected", "no_market", "unsolvable", "absent"):
                continue
            if not (r.bid and r.ask and r.ask >= r.bid > 0):
                continue
            if r.delta is None:
                continue
            out.append(r)
        return out

    def stats_dict(self) -> dict:
        return {
            "polls": self.stats.polls,
            "failures": self.stats.failures,
            "rate_limited": self.stats.rate_limited,
            "greeks_repaired": self.stats.greeks_repaired,
            "greeks_rejected": self.stats.greeks_rejected,
            "last_error": self.stats.last_error,
            "age_sec": round(self.age_seconds(), 1),
            "fresh": self.is_fresh(),
        }
