"""Statistical forecasts of the NEXT bar's close: five small models on log returns.

Every model reads a 1-D array of closes (oldest first), works on the log returns
r_t = log(p_t / p_{t-1}) and gives the next return as a normal law N(mu, s^2) (except
`empirical`, which uses the returns' own percentiles). It is turned into prices at the end:

    mean  = last * exp(mu)              the expected LOG price (the price's median); the
                                        arithmetic mean is higher by exp(s^2 / 2), a few
                                        thousandths of a percent on these bars
    bands = last * exp(mu +- z * s)     z = 0.9945 (68%) and 1.96 (95%)
    sd    = mean * sqrt(exp(s^2) - 1)   the next close's standard deviation in rupees
    p_up  = P(r > 0) = Phi(mu / s)

Pure functions, no I/O. Bad data never raises: NaN and non-positive closes are dropped,
and too few bars, a constant series or a failed fit give a `Forecast` with a `note`.
They inform; nothing here raises an alert or a trade.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy import optimize, signal, stats  # type: ignore[import-untyped]

MODELS: tuple[str, ...] = ("arima", "sarima", "garch", "ewma", "empirical")
DESCRIPTIONS: dict[str, str] = {
    "arima": "ARIMA(1,1,1): ARMA(1,1) with a constant on log returns, least squares",
    "sarima": "ARIMA(1,1,1) plus one seasonal AR term at the session length",
    "garch": "GARCH(1,1) with a constant mean, maximum likelihood (normal)",
    "ewma": "Zero mean, RiskMetrics variance (lambda 0.94)",
    "empirical": "No model: percentiles of the last 250 returns",
}
#: bars in one NSE session per timeframe: the seasonal lag of `sarima`
SEASON: dict[str, int] = {"15m": 25, "30m": 13, "60m": 7, "4h": 2, "1d": 5}
#: closes a fitted model needs; `ewma` and `empirical` make do with `MIN_BARS_SIMPLE`
MIN_BARS = 60
MIN_BARS_SIMPLE = 30
#: the most recent returns a model is fitted on (older ones say little about the next bar)
MAX_RETURNS = 1000
EWMA_LAMBDA = 0.94
EMPIRICAL_WINDOW = 250
REFIT_EVERY = 20
Z68 = float(stats.norm.ppf(0.84))  # 0.9945: the 16th..84th percentile band
Z95 = float(stats.norm.ppf(0.975))  # 1.96
_BOUND = 0.98  # |phi|, |theta| and the seasonal coefficient stay inside this


@dataclass(frozen=True)
class Forecast:
    """The next bar's close as a distribution. All prices None when `note` says why."""

    mean: float | None = None
    sd: float | None = None
    p_up: float | None = None
    lo68: float | None = None
    hi68: float | None = None
    lo95: float | None = None
    hi95: float | None = None
    n: int = 0
    note: str | None = None
    #: the fit in a few words (parameters, or the simpler model it fell back to)
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── inputs ────────────────────────────────────────────────────────────────
def clean_closes(closes: Any) -> np.ndarray:
    """The finite, positive closes as floats (anything else is dropped)."""
    try:
        a = np.asarray(closes, dtype=float).ravel()
    except (TypeError, ValueError):
        return np.zeros(0)
    return a[np.isfinite(a) & (a > 0)]


def _returns(closes: np.ndarray) -> np.ndarray:
    return np.diff(np.log(closes))[-MAX_RETURNS:]


def _too_few(n: int, need: int) -> Forecast:
    return Forecast(n=n, note=f"too few bars to trust: {n} of {need}")


def _from_normal(last: float, mu: float, s: float, n: int, detail: str) -> Forecast:
    """Prices from a normal next log return N(mu, s^2)."""
    if not (math.isfinite(mu) and math.isfinite(s)) or s <= 0:
        return Forecast(n=n, note="the fit gave no usable variance", detail=detail)
    centre = last * math.exp(mu)
    return Forecast(
        mean=centre,
        sd=centre * math.sqrt(math.expm1(s * s)),
        p_up=float(stats.norm.cdf(mu / s)),
        lo68=last * math.exp(mu - Z68 * s),
        hi68=last * math.exp(mu + Z68 * s),
        lo95=last * math.exp(mu - Z95 * s),
        hi95=last * math.exp(mu + Z95 * s),
        n=n,
        detail=detail,
    )


# ── ARMA(1,1) (+ one seasonal AR term), conditional least squares ──────────
def _arma_residuals(r: np.ndarray, c: float, phi: float, theta: float, Phi: float, s: int):
    """e_t = r_t - c - phi r_{t-1} - Phi r_{t-s} - theta e_{t-1}, for t >= max(1, s)."""
    k = max(1, s)
    u = r[k:] - c - phi * r[k - 1 : -1]
    if Phi:
        u = u - Phi * r[k - s : len(r) - s]
    return signal.lfilter([1.0], [1.0, theta], u)


def _arma_fit(r: np.ndarray, s: int = 0) -> tuple[float, ...]:
    """(c, phi, theta, Phi) by conditional least squares; Phi is 0 when s is 0."""
    seasonal = s > 1
    scale = float(np.std(r)) or 1.0
    z = r / scale  # fitted on unit-variance returns: the optimiser's tolerances then fit

    def sse(x: np.ndarray) -> float:
        e = _arma_residuals(z, x[0], x[1], x[2], x[3] if seasonal else 0.0, s if seasonal else 0)
        return float(e @ e)

    x0 = np.array([float(np.mean(z)), 0.0, 0.0, 0.0][: 4 if seasonal else 3])
    bounds = [(-1.0, 1.0)] + [(-_BOUND, _BOUND)] * (3 if seasonal else 2)
    res = optimize.minimize(sse, x0, method="L-BFGS-B", bounds=bounds)
    x = res.x if np.all(np.isfinite(res.x)) and res.fun <= sse(x0) else x0
    return float(x[0]) * scale, float(x[1]), float(x[2]), float(x[3]) if seasonal else 0.0


def _stationary(phi: float, Phi: float, s: int) -> bool:
    """1 - phi z - Phi z^s has every root outside the unit circle."""
    if not Phi or s <= 1:
        return abs(phi) < 1
    poly = np.zeros(s + 1)
    poly[0], poly[1] = 1.0, -phi
    poly[s] += -Phi
    roots = np.roots(poly[::-1])
    return bool(np.all(np.abs(roots) > 1.0 + 1e-6))


def _arma_predict(r: np.ndarray, p: tuple[float, ...], s: int = 0) -> tuple[float, float]:
    """(mu, s) of the next return with fixed parameters: the recursion run over `r`."""
    c, phi, theta, Phi = p
    seas = s if Phi else 0
    e = _arma_residuals(r, c, phi, theta, Phi, seas)
    k = 3 if not Phi else 4
    sigma = math.sqrt(float(e @ e) / max(1, len(e) - k))
    mu = c + phi * r[-1] + theta * float(e[-1])
    if Phi:
        mu += Phi * r[-seas]
    return mu, sigma


# ── GARCH(1,1), maximum likelihood ────────────────────────────────────────
def _garch_var(e2: np.ndarray, omega: float, alpha: float, beta: float, v0: float) -> np.ndarray:
    """sigma2_t = omega + alpha e2_{t-1} + beta sigma2_{t-1}, sigma2_0 = v0; one value per
    e2 plus the next one (len(e2) + 1)."""
    x = np.empty(len(e2))
    x[0] = v0
    if len(e2) > 1:
        w = omega + alpha * e2[:-1]
        x[1:] = signal.lfilter([1.0], [1.0, -beta], w, zi=np.array([beta * v0]))[0]
    nxt = omega + alpha * e2[-1] + beta * x[-1]
    return np.append(x, nxt)


def _garch_unpack(x: np.ndarray, var: float) -> tuple[float, float, float, float]:
    """(mu, omega, alpha, beta) from the optimiser's (mu, log omega/var, persistence, share)."""
    persistence, share = float(x[2]), float(x[3])
    return float(x[0]), var * math.exp(float(x[1])), persistence * share, persistence * (1 - share)


def _garch_fit(r: np.ndarray) -> tuple[float, float, float, float]:
    """(mu, omega, alpha, beta) by normal maximum likelihood, started from the sample variance.
    omega > 0, alpha >= 0, beta >= 0 and alpha + beta < 1 hold by construction."""
    sd = float(np.std(r)) or 1.0
    z = r / sd  # fitted on unit-variance returns, then scaled back

    def nll(x: np.ndarray) -> float:
        mu, omega, alpha, beta = _garch_unpack(x, 1.0)
        e = z - mu
        v = _garch_var(e * e, omega, alpha, beta, 1.0)[:-1]
        if not np.all(v > 0):
            return 1e300
        return float(0.5 * np.sum(np.log(v) + e * e / v))

    x0 = np.array([float(np.mean(z)), math.log(0.05), 0.95, 0.1])
    bounds = [(-1.0, 1.0), (math.log(1e-6), math.log(10.0)), (0.0, 0.999), (0.0, 1.0)]
    res = optimize.minimize(nll, x0, method="L-BFGS-B", bounds=bounds)
    x = res.x if np.all(np.isfinite(res.x)) and res.fun <= nll(x0) else x0
    mu, omega, alpha, beta = _garch_unpack(x, 1.0)
    return mu * sd, omega * sd * sd, alpha, beta


def _garch_predict(r: np.ndarray, p: tuple[float, ...]) -> tuple[float, float]:
    mu, omega, alpha, beta = p
    e = r - mu
    v = _garch_var(e * e, omega, alpha, beta, float(np.var(r)))
    return mu, math.sqrt(float(v[-1]))


# ── the five models ───────────────────────────────────────────────────────
def _guard(closes: Any, need: int) -> tuple[np.ndarray, Forecast | None]:
    c = clean_closes(closes)
    if len(c) < need:
        return c, _too_few(len(c), need)
    r = _returns(c)
    if not np.any(r):
        return c, Forecast(n=len(c), note="the price has not moved: no variance to model")
    return c, None


def _arma_detail(p: tuple[float, ...]) -> str:
    c, phi, theta, Phi = p
    out = f"c={c:.2e} phi={phi:.3f} theta={theta:.3f}"
    return out + (f" seasonal={Phi:.3f}" if Phi else "")


def arima(closes: Any) -> Forecast:
    """ARIMA(1,1,1) on log prices: ARMA(1,1) with a constant on the returns."""
    c, bad = _guard(closes, MIN_BARS)
    if bad:
        return bad
    r = _returns(c)
    try:
        p = _arma_fit(r)
        mu, s = _arma_predict(r, p)
        return _from_normal(float(c[-1]), mu, s, len(c), _arma_detail(p))
    except Exception as exc:  # noqa: BLE001 - a forecast cell, never an error
        return Forecast(n=len(c), note=f"the fit failed: {type(exc).__name__}")


def sarima(closes: Any, season: int = 1) -> Forecast:
    """`arima` plus one seasonal AR term at lag `season` on the returns. Falls back to
    `arima` when the season is too long for the data or the fit is not stationary."""
    c, bad = _guard(closes, MIN_BARS)
    if bad:
        return bad
    r = _returns(c)
    s = int(season)
    try:
        if s > 1 and len(r) > 3 * s:
            p = _arma_fit(r, s)
            if _stationary(p[1], p[3], s):
                mu, sd = _arma_predict(r, p, s)
                return _from_normal(float(c[-1]), mu, sd, len(c), _arma_detail(p) + f" s={s}")
            why = "not stationary"
        else:
            why = f"too few bars for lag {s}" if s > 1 else "no season"
        p = _arma_fit(r)
        mu, sd = _arma_predict(r, p)
        return _from_normal(float(c[-1]), mu, sd, len(c), f"ARIMA fallback ({why})")
    except Exception as exc:  # noqa: BLE001
        return Forecast(n=len(c), note=f"the fit failed: {type(exc).__name__}")


def garch(closes: Any) -> Forecast:
    """GARCH(1,1) with a constant mean; the band is the next bar's conditional variance."""
    c, bad = _guard(closes, MIN_BARS)
    if bad:
        return bad
    r = _returns(c)
    try:
        p = _garch_fit(r)
        mu, s = _garch_predict(r, p)
        detail = f"omega={p[1]:.2e} alpha={p[2]:.3f} beta={p[3]:.3f}"
        return _from_normal(float(c[-1]), mu, s, len(c), detail)
    except Exception as exc:  # noqa: BLE001
        return Forecast(n=len(c), note=f"the fit failed: {type(exc).__name__}")


def ewma_variance(r: np.ndarray, lam: float = EWMA_LAMBDA) -> float:
    """RiskMetrics: v <- lam v + (1 - lam) r_t^2 over the returns, seeded with mean(r^2)."""
    r = np.asarray(r, dtype=float)
    v0 = float(np.mean(r * r))
    y, _ = signal.lfilter([1 - lam], [1.0, -lam], r * r, zi=[lam * v0])
    return float(y[-1])


def ewma(closes: Any) -> Forecast:
    """Zero mean, exponentially weighted variance (lambda 0.94)."""
    c, bad = _guard(closes, MIN_BARS_SIMPLE)
    if bad:
        return bad
    v = ewma_variance(_returns(c))
    return _from_normal(float(c[-1]), 0.0, math.sqrt(v), len(c), f"lambda={EWMA_LAMBDA}")


def empirical(closes: Any) -> Forecast:
    """No model: the 2.5/16/50/84/97.5 percentiles of the last 250 returns give the bands
    (the 50th is the centre), the share of positive returns gives p_up."""
    c, bad = _guard(closes, MIN_BARS_SIMPLE)
    if bad:
        return bad
    r = _returns(c)[-EMPIRICAL_WINDOW:]
    q = np.percentile(r, [2.5, 16, 50, 84, 97.5])
    last = float(c[-1])
    lo95, lo68, mid, hi68, hi95 = (last * math.exp(float(x)) for x in q)
    return Forecast(
        mean=mid,
        sd=float(np.std(last * np.exp(r))),
        p_up=float(np.mean(r > 0)),
        lo68=lo68,
        hi68=hi68,
        lo95=lo95,
        hi95=hi95,
        n=len(c),
        detail=f"{len(r)} returns",
    )


def forecast(model: str, closes: Any, season: int = 1) -> Forecast:
    """One model by name."""
    if model == "sarima":
        return sarima(closes, season)
    fn: dict[str, Callable[[Any], Forecast]] = {
        "arima": arima,
        "garch": garch,
        "ewma": ewma,
        "empirical": empirical,
    }
    if model not in fn:
        return Forecast(note=f"unknown model {model}")
    return fn[model](closes)


# ── walk-forward backtest ─────────────────────────────────────────────────
def _stepper(model: str, season: int) -> Callable[[np.ndarray, Any], tuple[Forecast, Any]]:
    """step(closes, params) -> (forecast from these closes, params to reuse). Fitted models
    re-fit when params is None and otherwise only run their recursion forward."""
    s = int(season)

    def step(c: np.ndarray, p: Any) -> tuple[Forecast, Any]:
        need = MIN_BARS if model in ("arima", "sarima", "garch") else MIN_BARS_SIMPLE
        if len(c) < need:
            return _too_few(len(c), need), None
        if model in ("ewma", "empirical"):
            return forecast(model, c), None
        r = _returns(c)
        if not np.any(r):
            return Forecast(n=len(c), note="the price has not moved"), None
        last = float(c[-1])
        if model == "garch":
            p = p or _garch_fit(r)
            mu, sd = _garch_predict(r, p)
            return _from_normal(last, mu, sd, len(c), "garch"), p
        seas = s if model == "sarima" and s > 1 and len(r) > 3 * s else 0
        if p is None:
            p = _arma_fit(r, seas)
            if seas and not _stationary(p[1], p[3], seas):
                p = _arma_fit(r)
        mu, sd = _arma_predict(r, p, seas)
        return _from_normal(last, mu, sd, len(c), model), p

    return step


def walk_forward(
    closes: Any, model: str, last: int = 100, season: int = 1
) -> list[tuple[Forecast, float, float]]:
    """(forecast for bar t from bars up to t-1, close of t-1, close of t) for each of the
    last `last` bars. Parameters are re-fitted every `REFIT_EVERY` steps on the bars before
    the step, and the recursions run forward with them in between: no look-ahead."""
    c = clean_closes(closes)
    first = max(1, len(c) - int(last))
    step = _stepper(model, season)
    out: list[tuple[Forecast, float, float]] = []
    params: Any = None
    for k, t in enumerate(range(first, len(c))):
        if k % REFIT_EVERY == 0:
            params = None
        try:
            fc, params = step(c[:t], params)
        except Exception as exc:  # noqa: BLE001
            fc, params = Forecast(n=t, note=f"the fit failed: {type(exc).__name__}"), None
        out.append((fc, float(c[t - 1]), float(c[t])))
    return out


def _inside(x: float, lo: float | None, hi: float | None) -> int:
    return int(lo is not None and hi is not None and lo <= x <= hi)


def backtest(closes: Any, model: str, last: int = 100, season: int = 1) -> dict[str, Any]:
    """How the last `last` forecasts did: the share of next closes inside the 68% and 95%
    bands, and the share of moves whose sign matched sign(mean - last close) (steps where
    either is zero are left out; a zero-mean model has no direction). None: no steps."""
    steps = [(f, p, x) for f, p, x in walk_forward(closes, model, last, season) if f.note is None]
    if not steps:
        return {"steps": 0, "in68": None, "in95": None, "direction": None}
    in68 = sum(_inside(x, f.lo68, f.hi68) for f, _, x in steps)
    in95 = sum(_inside(x, f.lo95, f.hi95) for f, _, x in steps)
    called = [
        (np.sign(f.mean - p), np.sign(x - p))
        for f, p, x in steps
        if f.mean is not None and f.mean != p and x != p
    ]
    right = sum(1 for a, b in called if a == b)
    return {
        "steps": len(steps),
        "in68": in68 / len(steps),
        "in95": in95 / len(steps),
        "direction": right / len(called) if called else None,
    }
