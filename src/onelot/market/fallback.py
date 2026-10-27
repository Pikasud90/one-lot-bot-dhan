"""Free, no-credential fallback data source (Yahoo Finance).

WHAT THIS IS FOR — and, more importantly, what it is NOT for.

It supplies INDEX data only:
  * daily bars      — regime training history, without touching broker quota
  * 5-minute bars   — indicator warm-up, same reason
  * a spot level    — an INDEPENDENT cross-check on the broker's index price

It cannot supply an option chain, greeks, option quotes, bid/ask depth or open
interest. Those come from the broker or they do not come at all. So this is
explicitly NOT a trading feed: if the broker's chain dies, the engine goes
DEGRADED and stops signalling, and no amount of Yahoo data changes that. The
honest value here is threefold:

  1. Warm-up and history for free, so a cold start does not burn broker quota
     you may need later in the session.
  2. A second opinion on the index level. A broker feed that is stuck — still
     connected, still returning the same number — is invisible to a staleness
     check but obvious against an independent source. That is a real failure
     mode and this is the cheapest way to catch it.
  3. Something to work with when you have no broker subscription at all: the
     backtest and warm-up paths run without credentials.

Polling is deliberately rare (default 5 minutes, floor of 60 seconds enforced
by config). This is a safety net. Hammering a free public endpoint would get
it blocked and would buy nothing — the broker is the low-latency path.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from ..broker.base import Bar
from ..clock import IST, to_ist

log = logging.getLogger(__name__)

# Yahoo Finance tickers for the instruments we care about.
YF_SYMBOLS = {
    "BANKNIFTY": "^NSEBANK",
    "NIFTY": "^NSEI",
    "INDIAVIX": "^INDIAVIX",
}


class FallbackUnavailable(RuntimeError):
    pass


class YFinanceFallback:
    """Thin, defensive wrapper. Every method degrades to empty rather than raising."""

    name = "yfinance"

    def __init__(self, symbol: str = "BANKNIFTY",
                 poll_interval_sec: float = 300.0,
                 divergence_pct: float = 0.005):
        self.symbol = symbol
        self.ticker = YF_SYMBOLS.get(symbol.upper(), symbol)
        self.poll_interval_sec = poll_interval_sec
        self.divergence_pct = divergence_pct
        self._lock = threading.Lock()
        self._last_price: float | None = None
        self._last_price_at: dt.datetime | None = None
        self.available = self._probe()
        self.last_error: str | None = None
        self.divergence_warnings = 0

    def _probe(self) -> bool:
        try:
            import yfinance  # noqa: F401
            return True
        except ImportError:
            log.info("yfinance not installed — the free fallback is disabled. "
                     "The engine works without it; you lose the independent "
                     "index cross-check and free history backfill.")
            return False

    # ── history ───────────────────────────────────────────────────────────
    def _download(self, period: str, interval: str):
        import yfinance as yf
        return yf.Ticker(self.ticker).history(
            period=period, interval=interval, auto_adjust=False)

    def _to_bars(self, frame, has_volume: bool) -> list[Bar]:
        out: list[Bar] = []
        if frame is None or len(frame) == 0:
            return out
        for idx, row in frame.iterrows():
            try:
                ts = to_ist(idx.to_pydatetime())
            except Exception:
                continue
            try:
                o, h, low, c = (float(row["Open"]), float(row["High"]),
                                float(row["Low"]), float(row["Close"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not all(v > 0 for v in (o, h, low, c)):
                continue
            vol = None
            if has_volume:
                try:
                    v = float(row.get("Volume", 0) or 0)
                    # An index reports 0. Store NULL so VWAP cannot compute on
                    # 0/0 — the same rule the broker adapter follows.
                    vol = v if v > 0 else None
                except (TypeError, ValueError):
                    vol = None
            out.append(Bar(ts=ts, open=o, high=h, low=low, close=c, volume=vol))
        return out

    def daily_bars(self, days: int = 400) -> list[Bar]:
        """Daily index history, for regime training and IV-percentile context."""
        if not self.available:
            return []
        period = "2y" if days > 365 else ("1y" if days > 180 else "6mo")
        try:
            return self._to_bars(self._download(period, "1d"), has_volume=False)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("yfinance daily fetch failed: %s", self.last_error)
            return []

    def intraday_bars(self, interval_min: int = 5, days: int = 5) -> list[Bar]:
        """Intraday index bars for warm-up.

        Yahoo caps intraday history: roughly 60 days at 5m, 7 days at 1m. That
        is comfortably more than the warm-up needs.
        """
        if not self.available:
            return []
        interval = f"{interval_min}m"
        period = f"{min(max(days, 1), 58)}d"
        try:
            return self._to_bars(self._download(period, interval), has_volume=False)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("yfinance intraday fetch failed: %s", self.last_error)
            return []

    # ── cross-check ───────────────────────────────────────────────────────
    def index_price(self, now: dt.datetime | None = None) -> float | None:
        """Cached spot level. Refuses to poll faster than the configured rate."""
        if not self.available:
            return None
        now = now or dt.datetime.now(IST)
        with self._lock:
            fresh = (self._last_price_at is not None
                     and (now - self._last_price_at).total_seconds()
                     < self.poll_interval_sec)
            if fresh:
                return self._last_price
        try:
            bars = self._to_bars(self._download("1d", "5m"), has_volume=False)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if not bars:
            return None
        with self._lock:
            self._last_price = bars[-1].close
            self._last_price_at = now
            return self._last_price

    def check_divergence(self, broker_price: float | None,
                         now: dt.datetime | None = None) -> tuple[bool, str]:
        """Compare the broker's index level against an independent source.

        Catches the failure a staleness check cannot see: a feed that is still
        connected and still returning a number, but the wrong one.
        """
        if not self.available or not broker_price or broker_price <= 0:
            return True, "cross-check unavailable"
        ref = self.index_price(now)
        if not ref or ref <= 0:
            return True, "no reference price"
        diff = abs(broker_price - ref) / ref
        if diff > self.divergence_pct:
            self.divergence_warnings += 1
            return False, (
                f"broker index {broker_price:,.0f} vs {self.name} {ref:,.0f} "
                f"differ by {diff:.2%} (limit {self.divergence_pct:.2%}) — "
                "one of the two feeds is wrong; verify on your terminal")
        return True, f"agrees within {diff:.3%}"

    def status(self) -> dict:
        return {
            "source": self.name, "ticker": self.ticker,
            "available": self.available,
            "last_price": self._last_price,
            "last_price_at": (self._last_price_at.isoformat()
                              if self._last_price_at else None),
            "divergence_warnings": self.divergence_warnings,
            "last_error": self.last_error,
            "note": "index data only — cannot supply an option chain or greeks",
        }


def build_fallback(cfg) -> YFinanceFallback | None:
    if cfg.data.fallback_source != "yfinance":
        return None
    return YFinanceFallback(
        symbol=cfg.instrument.underlying_symbol,
        poll_interval_sec=cfg.data.fallback_poll_interval_sec,
        divergence_pct=cfg.data.fallback_divergence_pct,
    )
