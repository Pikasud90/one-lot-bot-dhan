"""Black-76 option pricing, implied volatility and greeks.

Why Black-76 and not Black-Scholes:
    NSE index options are European and priced off the FUTURES, not the spot.
    Discounting a spot price at an assumed risk-free rate to synthesise a
    forward introduces a systematic error that is largest exactly where this
    strategy operates — ITM strikes with meaningful time value. Black-76 takes
    the forward directly, which is observable.

Why we compute these at all when the broker supplies greeks (v1 defect A9):
    A stale, null or wrong greek from the feed would size and filter the whole
    trade off garbage with no detection. Local values give us a cross-check,
    and a fallback when the feed omits them.

No SciPy dependency in the hot path: the normal CDF comes from math.erf and
implied vol from a bracketed bisection, which cannot fail to converge or throw.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Literal

OptionType = Literal["CE", "PE"]

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
_MIN_T = 1e-8
_MIN_SIGMA = 1e-6


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def year_fraction(now: dt.datetime, expiry: dt.datetime) -> float:
    """Calendar-time year fraction. Options decay on the wall clock, including
    weekends, which is precisely why a Friday-to-Monday hold bleeds."""
    return max((expiry - now).total_seconds(), 0.0) / SECONDS_PER_YEAR


def _d1_d2(f: float, k: float, t: float, sigma: float) -> tuple[float, float]:
    v = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / v
    return d1, d1 - v


def price(f: float, k: float, t: float, sigma: float, r: float = 0.0,
          opt: OptionType = "CE") -> float:
    """Black-76 fair value. Degrades to intrinsic at expiry rather than dividing by zero."""
    if f <= 0 or k <= 0:
        return 0.0
    disc = math.exp(-r * t)
    if t <= _MIN_T or sigma <= _MIN_SIGMA:
        intrinsic = (f - k) if opt == "CE" else (k - f)
        return max(intrinsic, 0.0) * disc
    d1, d2 = _d1_d2(f, k, t, sigma)
    if opt == "CE":
        return disc * (f * norm_cdf(d1) - k * norm_cdf(d2))
    return disc * (k * norm_cdf(-d2) - f * norm_cdf(-d1))


@dataclass(frozen=True)
class Greeks:
    """Greeks in the units a human reads.

    delta : per 1 point of underlying. CE positive, PE NEGATIVE — the sign that
            broke v1's contract filter (defect A8). Compare against abs(delta).
    gamma : delta change per 1 point of underlying
    vega  : rupees per 1 VOLATILITY POINT (1%), not per 1.00 of sigma
    theta : rupees per CALENDAR DAY (negative for a long option)
    """

    delta: float
    gamma: float
    vega: float
    theta: float
    theta_per_hour: float
    iv: float
    price: float

    def as_dict(self) -> dict[str, float]:
        return {
            "delta": round(self.delta, 5), "gamma": round(self.gamma, 7),
            "vega": round(self.vega, 4), "theta": round(self.theta, 4),
            "theta_per_hour": round(self.theta_per_hour, 4),
            "iv": round(self.iv, 5), "price": round(self.price, 4),
        }


def greeks(f: float, k: float, t: float, sigma: float, r: float = 0.0,
           opt: OptionType = "CE") -> Greeks:
    """Analytic Black-76 greeks."""
    if f <= 0 or k <= 0 or t <= _MIN_T or sigma <= _MIN_SIGMA:
        intrinsic_delta = 0.0
        if t <= _MIN_T:
            if opt == "CE":
                intrinsic_delta = 1.0 if f > k else 0.0
            else:
                intrinsic_delta = -1.0 if f < k else 0.0
        return Greeks(intrinsic_delta, 0.0, 0.0, 0.0, 0.0, max(sigma, 0.0),
                      price(f, k, t, sigma, r, opt))

    disc = math.exp(-r * t)
    sqrt_t = math.sqrt(t)
    d1, d2 = _d1_d2(f, k, t, sigma)
    nd1 = norm_pdf(d1)

    if opt == "CE":
        delta = disc * norm_cdf(d1)
    else:
        delta = -disc * norm_cdf(-d1)

    gamma = disc * nd1 / (f * sigma * sqrt_t)
    vega_per_unit_sigma = f * disc * nd1 * sqrt_t

    px = price(f, k, t, sigma, r, opt)
    # theta = -dV/dT ; in Black-76 the F*n(d1) = K*n(d2) identity collapses the
    # first term to a form identical for calls and puts.
    theta_per_year = -(f * disc * nd1 * sigma) / (2.0 * sqrt_t) + r * px

    return Greeks(
        delta=delta,
        gamma=gamma,
        vega=vega_per_unit_sigma / 100.0,     # per 1 vol point
        theta=theta_per_year / 365.0,          # per calendar day
        theta_per_hour=theta_per_year / (365.0 * 24.0),
        iv=sigma,
        price=px,
    )


def implied_vol(market_price: float, f: float, k: float, t: float, r: float = 0.0,
                opt: OptionType = "CE", lo: float = 1e-4, hi: float = 5.0,
                tol: float = 1e-7, max_iter: int = 200) -> float:
    """Implied volatility by bracketed bisection.

    Bisection over Newton deliberately: option vega vanishes for deep ITM/OTM
    strikes, where Newton diverges or returns absurd values. Bisection cannot
    diverge, cannot throw, and returns NaN honestly when the price is outside
    the no-arbitrage bounds.
    """
    if market_price <= 0 or f <= 0 or k <= 0 or t <= _MIN_T:
        return float("nan")

    disc = math.exp(-r * t)
    intrinsic = max((f - k) if opt == "CE" else (k - f), 0.0) * disc
    upper_bound = (f * disc) if opt == "CE" else (k * disc)
    if market_price < intrinsic - 1e-6 or market_price > upper_bound + 1e-6:
        return float("nan")

    p_lo = price(f, k, t, lo, r, opt)
    p_hi = price(f, k, t, hi, r, opt)
    if market_price <= p_lo:
        return lo
    if market_price >= p_hi:
        return float("nan")   # beyond 500% vol: the quote is not believable

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        pm = price(f, k, t, mid, r, opt)
        if abs(pm - market_price) < tol or (hi - lo) < tol:
            return mid
        if pm < market_price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def implied_forward(call_price: float, put_price: float, k: float, t: float,
                    r: float = 0.0) -> float:
    """Forward implied by put-call parity: F = K + e^{rT}(C - P).

    A useful cross-check on the futures quote, and a fallback when the futures
    feed is stale. Both being wrong the same way is unlikely.
    """
    return k + math.exp(r * t) * (call_price - put_price)


def greeks_from_quote(market_price: float, f: float, k: float, t: float,
                      r: float = 0.0, opt: OptionType = "CE") -> Greeks | None:
    """Solve IV from a traded price, then return the full greek set."""
    iv = implied_vol(market_price, f, k, t, r, opt)
    if math.isnan(iv):
        return None
    return greeks(f, k, t, iv, r, opt)


def cross_check_delta(broker_delta: float | None, local: Greeks | None,
                      tolerance: float) -> tuple[bool, str]:
    """Validate a broker-supplied delta against our own (v1 defect A9).

    Returns (trustworthy, reason). A missing broker greek is fine — we use the
    local one. A broker greek that disagrees materially is NOT fine: one of the
    two is wrong and we cannot tell which, so the contract is skipped.
    """
    if local is None:
        return False, "local greeks unavailable (IV solve failed)"
    if broker_delta is None:
        return True, "broker delta absent; using local"
    diff = abs(abs(broker_delta) - abs(local.delta))
    if diff > tolerance:
        return False, (f"broker delta {broker_delta:+.4f} vs local {local.delta:+.4f} "
                       f"differ by {diff:.4f} > {tolerance}")
    return True, f"agree within {diff:.4f}"
