"""Hidden Markov regime detection — with v1's look-ahead leaks closed.

*** v1 defect C1 — THE LOOK-AHEAD LEAK ***
v1 called `model.predict(X)`. That runs VITERBI, which finds the most likely
state sequence GIVEN THE ENTIRE OBSERVATION SEQUENCE — including the future.
Labelling "today's regime" that way means today's label depends on tomorrow's
data. Backtests glow; live trading does not match. It is the single most
common way an HMM trading system fools its author.

`predict_proba` is no better: it returns the SMOOTHED posterior
P(state_t | all observations), which is also conditioned on the future.

The fix is FILTERING, not smoothing: P(state_t | observations up to t), from a
forward pass only. Implemented here explicitly, with a test that asserts a
day's label is unchanged when future data is appended.

*** v1 defect C2 — UNSTABLE STATE LABELS ***
hmmlearn state indices are arbitrary. State 0 is not "bull", it is whatever EM
converged to, and the permutation can change on every refit. v1 assumed stable
semantics, so an overnight refit could silently turn the bull filter into the
bear filter. States are relabelled deterministically by mean return here.

*** v1 defect C3 — TIMESCALE MISMATCH ***
A daily regime persists for days to weeks; using one to authorise a 5-minute
entry means the "filter" is constant all day. The daily HMM now supplies only
DIRECTIONAL BIAS; a separate intraday chop filter does the per-bar gating.

Regime for trading day T is inferred from data through T-1. Today's incomplete
bar is never used.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import pickle
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

from .indicators import ema, rolling_percentile_of_last

log = logging.getLogger(__name__)


class Regime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    CHOP = "CHOP"
    UNKNOWN = "UNKNOWN"

    def allows(self, direction: str) -> bool:
        """CE is permitted only in BULL, PE only in BEAR. CHOP blocks both —
        buying premium in a range is a slow donation to theta."""
        if self is Regime.BULL:
            return direction == "CE"
        if self is Regime.BEAR:
            return direction == "PE"
        return False


@dataclass
class RegimeResult:
    regime: Regime
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    method: str = "hmm"
    model_hash: str | None = None
    as_of: str | None = None          # the DATE whose data produced this
    note: str = ""

    def as_dict(self) -> dict:
        return {"regime": self.regime.value, "confidence": round(self.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
                "method": self.method, "model_hash": self.model_hash,
                "as_of": self.as_of, "note": self.note}


# ─────────────────────────── features ─────────────────────────────────────

def build_features(closes: np.ndarray, vol_window: int = 10) -> np.ndarray:
    """Two features per session: log return, and log realised volatility.

    Returns an (n-vol_window, 2) matrix aligned to closes[vol_window:].
    """
    closes = np.asarray(closes, dtype=float)
    if len(closes) < vol_window + 2:
        return np.empty((0, 2))
    logret = np.diff(np.log(closes))
    feats = []
    for i in range(vol_window, len(logret)):
        window = logret[i - vol_window:i]
        rv = float(np.std(window))
        feats.append([logret[i], np.log(max(rv, 1e-6))])
    return np.asarray(feats, dtype=float)


def feature_dates(dates: list[dt.date], vol_window: int = 10) -> list[dt.date]:
    """Dates aligned to `build_features` output."""
    return list(dates[vol_window + 1:])


# ─────────────────────────── forward filtering ────────────────────────────

def _log_gaussian_diag(x: np.ndarray, means: np.ndarray, covars: np.ndarray) -> np.ndarray:
    """Log emission probabilities for a diagonal-covariance Gaussian HMM.

    Implemented here rather than via hmmlearn's private
    `_compute_log_likelihood`, which has moved between versions. A routine
    dependency upgrade must not silently change how regimes are computed.
    """
    n, d = x.shape
    k = means.shape[0]
    out = np.empty((n, k))
    for j in range(k):
        var = np.maximum(covars[j].reshape(-1)[:d], 1e-12)
        diff = x - means[j]
        out[:, j] = -0.5 * (
            d * np.log(2 * np.pi) + np.sum(np.log(var)) + np.sum(diff * diff / var, axis=1)
        )
    return out


def _logsumexp(a: np.ndarray, axis=None, keepdims=False) -> np.ndarray:
    m = np.max(a, axis=axis, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    out = m + np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True))
    return out if keepdims else np.squeeze(out, axis=axis)


def filtered_state_probabilities(framelogprob: np.ndarray, startprob: np.ndarray,
                                 transmat: np.ndarray) -> np.ndarray:
    """FORWARD-ONLY filtering: returns P(state_t | observations 1..t).

    This is the function that closes v1 defect C1. Note there is no backward
    pass — by construction the result at time t cannot depend on t+1.
    """
    n, k = framelogprob.shape
    log_start = np.log(np.maximum(startprob, 1e-300))
    log_trans = np.log(np.maximum(transmat, 1e-300))

    log_alpha = np.empty((n, k))
    log_alpha[0] = log_start + framelogprob[0]
    for t in range(1, n):
        for j in range(k):
            log_alpha[t, j] = _logsumexp(log_alpha[t - 1] + log_trans[:, j]) + framelogprob[t, j]

    norm = _logsumexp(log_alpha, axis=1, keepdims=True)
    return np.exp(log_alpha - norm)


# ─────────────────────────── the model ────────────────────────────────────

class RegimeModel:
    """Daily 3-state Gaussian HMM with deterministic, stable labelling."""

    def __init__(self, n_states: int = 3, vol_window: int = 10,
                 min_confidence: float = 0.60, random_state: int = 42,
                 n_iter: int = 200):
        self.n_states = n_states
        self.vol_window = vol_window
        self.min_confidence = min_confidence
        self.random_state = random_state
        self.n_iter = n_iter

        self._means: np.ndarray | None = None
        self._covars: np.ndarray | None = None
        self._startprob: np.ndarray | None = None
        self._transmat: np.ndarray | None = None
        self._label_map: dict[int, Regime] = {}
        self.model_hash: str | None = None
        self.fitted_through: str | None = None
        self.fit_note: str = ""

    @property
    def is_fitted(self) -> bool:
        return self._means is not None

    # ── fitting ───────────────────────────────────────────────────────────
    def fit(self, closes: np.ndarray, through_date: dt.date | None = None) -> bool:
        """Fit on the supplied history. The CALLER is responsible for passing
        only data up to and including T-1 — see `RegimeEngine.compute_for`."""
        feats = build_features(closes, self.vol_window)
        if len(feats) < max(40, self.n_states * 12):
            self.fit_note = f"insufficient history: {len(feats)} feature rows"
            log.warning("Regime fit skipped — %s", self.fit_note)
            return False
        try:
            from hmmlearn.hmm import GaussianHMM
        except ImportError:
            self.fit_note = "hmmlearn not installed"
            log.warning("hmmlearn unavailable — regime falls back to rule-based")
            return False

        try:
            import warnings
            with warnings.catch_warnings():
                # hmmlearn emits a ConvergenceMonitor warning when EM plateaus.
                # That is normal on noisy financial data and is not an error;
                # what matters is whether the fitted parameters are usable,
                # which the label map and confidence floor already police.
                warnings.simplefilter("ignore")
                m = GaussianHMM(n_components=self.n_states, covariance_type="diag",
                                n_iter=self.n_iter, random_state=self.random_state,
                                init_params="stmc")
                m.fit(feats)
        except Exception as exc:
            self.fit_note = f"HMM fit failed: {type(exc).__name__}: {exc}"
            log.warning(self.fit_note)
            return False

        self._means = np.asarray(m.means_, dtype=float)
        self._covars = np.asarray(m.covars_, dtype=float)
        if self._covars.ndim == 3:                      # (k, d, d) -> diagonal
            self._covars = np.array([np.diag(c) for c in self._covars])
        self._startprob = np.asarray(m.startprob_, dtype=float)
        self._transmat = np.asarray(m.transmat_, dtype=float)
        self._label_map = self._build_label_map(self._means)
        self.model_hash = self._hash()
        self.fitted_through = through_date.isoformat() if through_date else None
        self.fit_note = "ok"

        log.info("Regime model fitted (%d rows, hash %s). States: %s",
                 len(feats), self.model_hash,
                 {self._label_map[i].value: f"mean_ret={self._means[i][0]:+.5f}"
                  for i in range(self.n_states)})
        return True

    def _build_label_map(self, means: np.ndarray) -> dict[int, Regime]:
        """Deterministic relabelling by mean daily return (v1 defect C2).

        Highest mean return -> BULL, lowest -> BEAR, remainder -> CHOP.
        Applied on EVERY fit, so semantics survive a refit.
        """
        order = np.argsort(means[:, 0])          # ascending mean return
        mapping: dict[int, Regime] = {}
        mapping[int(order[0])] = Regime.BEAR
        mapping[int(order[-1])] = Regime.BULL
        for idx in order[1:-1]:
            mapping[int(idx)] = Regime.CHOP
        return mapping

    def _hash(self) -> str:
        h = hashlib.sha256()
        for arr in (self._means, self._covars, self._startprob, self._transmat):
            h.update(np.ascontiguousarray(np.round(arr, 8)).tobytes())
        return h.hexdigest()[:16]

    # ── inference ─────────────────────────────────────────────────────────
    def infer(self, closes: np.ndarray) -> RegimeResult:
        """Filtered regime for the LAST observation in `closes`.

        Uses a forward pass only, so appending future data cannot change this
        result — the property `tests/test_regime.py` asserts directly.
        """
        if not self.is_fitted:
            return RegimeResult(Regime.UNKNOWN, 0.0, method="unfitted",
                                note=self.fit_note or "model not fitted")
        feats = build_features(closes, self.vol_window)
        if len(feats) == 0:
            return RegimeResult(Regime.UNKNOWN, 0.0, method="hmm",
                                note="not enough observations to build features")

        flp = _log_gaussian_diag(feats, self._means, self._covars)
        filt = filtered_state_probabilities(flp, self._startprob, self._transmat)
        last = filt[-1]

        probs: dict[str, float] = {}
        for idx, p in enumerate(last):
            label = self._label_map[idx].value
            probs[label] = probs.get(label, 0.0) + float(p)

        best_label = max(probs, key=probs.get)
        conf = probs[best_label]

        if conf < self.min_confidence:
            # v1 had no confidence floor: a 34/33/33 split counted as a regime.
            return RegimeResult(Regime.CHOP, conf, probs, "hmm", self.model_hash,
                                note=f"max confidence {conf:.2f} < {self.min_confidence} "
                                     f"— treated as CHOP")
        return RegimeResult(Regime(best_label), conf, probs, "hmm", self.model_hash)

    # ── persistence ───────────────────────────────────────────────────────
    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(pickle.dumps({
            "n_states": self.n_states, "vol_window": self.vol_window,
            "min_confidence": self.min_confidence, "means": self._means,
            "covars": self._covars, "startprob": self._startprob,
            "transmat": self._transmat,
            "label_map": {k: v.value for k, v in self._label_map.items()},
            "model_hash": self.model_hash, "fitted_through": self.fitted_through,
        }))

    @classmethod
    def load(cls, path: str | Path) -> RegimeModel | None:
        p = Path(path)
        if not p.exists():
            return None
        try:
            d = pickle.loads(p.read_bytes())
        except Exception as exc:
            log.warning("Could not load regime model: %s", exc)
            return None
        m = cls(n_states=d["n_states"], vol_window=d["vol_window"],
                min_confidence=d["min_confidence"])
        m._means, m._covars = d["means"], d["covars"]
        m._startprob, m._transmat = d["startprob"], d["transmat"]
        m._label_map = {int(k): Regime(v) for k, v in d["label_map"].items()}
        m.model_hash, m.fitted_through = d["model_hash"], d["fitted_through"]
        m.fit_note = "loaded"
        return m


# ─────────────────────────── fallback ─────────────────────────────────────

def rule_based_regime(closes: np.ndarray, fast: int = 21, slow: int = 50,
                      slope_lookback: int = 5) -> RegimeResult:
    """Deterministic fallback when hmmlearn is unavailable or the fit fails.

    Deliberately crude: a fast/slow EMA relationship plus slope. Being obviously
    simple is a feature — nobody should mistake it for the model.
    """
    closes = np.asarray(closes, dtype=float)
    if len(closes) < slow + slope_lookback + 1:
        return RegimeResult(Regime.UNKNOWN, 0.0, method="rule_based",
                            note="insufficient history")
    f, s = ema(closes, fast), ema(closes, slow)
    if np.isnan(f[-1]) or np.isnan(s[-1]) or np.isnan(f[-1 - slope_lookback]):
        return RegimeResult(Regime.UNKNOWN, 0.0, method="rule_based",
                            note="indicators undefined")
    spread = (f[-1] - s[-1]) / s[-1]
    slope = (f[-1] - f[-1 - slope_lookback]) / f[-1 - slope_lookback]
    conf = float(min(1.0, abs(spread) * 120.0))
    if spread > 0.002 and slope > 0:
        return RegimeResult(Regime.BULL, max(conf, 0.6), method="rule_based",
                            note="EMA21 above EMA50 and rising")
    if spread < -0.002 and slope < 0:
        return RegimeResult(Regime.BEAR, max(conf, 0.6), method="rule_based",
                            note="EMA21 below EMA50 and falling")
    return RegimeResult(Regime.CHOP, 0.5, method="rule_based", note="no clear trend")


# ─────────────────────────── intraday filter ──────────────────────────────

@dataclass
class IntradayChopResult:
    is_trending: bool
    adx: float
    atr_percentile: float
    reason: str

    def as_dict(self) -> dict:
        return {"is_trending": self.is_trending, "adx": round(self.adx, 2),
                "atr_percentile": round(self.atr_percentile, 1), "reason": self.reason}


def intraday_chop_filter(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                         adx_min: float = 20.0, atr_pct_min: float = 20.0,
                         atr_pct_max: float = 90.0, adx_period: int = 14,
                         atr_period: int = 14,
                         atr_lookback: int = 200) -> IntradayChopResult:
    """The per-bar gate the daily HMM cannot provide (v1 defect C3).

    Two conditions:
      ADX above a floor            — there is a trend at all
      ATR percentile inside a band — volatility is neither dead (nothing moves,
                                     theta wins) nor extreme (stops get run)
    """
    from .indicators import adx as adx_fn
    from .indicators import atr as atr_fn

    a, _, _ = adx_fn(high, low, close, adx_period)
    cur_adx = float(a[-1]) if len(a) and not np.isnan(a[-1]) else float("nan")

    atr_series = atr_fn(high, low, close, atr_period)
    atr_pct = rolling_percentile_of_last(atr_series, atr_lookback)

    if np.isnan(cur_adx):
        return IntradayChopResult(False, float("nan"), atr_pct,
                                  "ADX undefined — insufficient bars (warm-up)")
    if cur_adx < adx_min:
        return IntradayChopResult(False, cur_adx, atr_pct,
                                  f"ADX {cur_adx:.1f} < {adx_min} — intraday chop")
    if not np.isnan(atr_pct):
        if atr_pct < atr_pct_min:
            return IntradayChopResult(False, cur_adx, atr_pct,
                                      f"ATR percentile {atr_pct:.0f} < {atr_pct_min} "
                                      "— too quiet to cover costs")
        if atr_pct > atr_pct_max:
            return IntradayChopResult(False, cur_adx, atr_pct,
                                      f"ATR percentile {atr_pct:.0f} > {atr_pct_max} "
                                      "— volatility extreme, stops will be run")
    return IntradayChopResult(True, cur_adx, atr_pct,
                              f"ADX {cur_adx:.1f}, ATR pct {atr_pct:.0f}")


# ─────────────────────────── orchestration ────────────────────────────────

def compute_for_date(dates: list[dt.date], closes: np.ndarray, target_date: dt.date,
                     model: RegimeModel | None = None,
                     fallback: str = "rule_based",
                     engine: str = "rule_based") -> tuple[RegimeResult, RegimeModel | None]:
    """Regime for `target_date`, using ONLY data strictly before it.

    This function is where v1 defect C1 is actually prevented. It slices the
    history to `target_date - 1` before either fitting or inferring, so no code
    path downstream can accidentally see the future — including today's own
    incomplete session.

    Returns (result, fitted_model).
    """
    dates = list(dates)
    closes = np.asarray(closes, dtype=float)
    if len(dates) != len(closes):
        raise ValueError(f"dates ({len(dates)}) and closes ({len(closes)}) must align")

    keep = [i for i, d in enumerate(dates) if d < target_date]
    if not keep:
        return RegimeResult(Regime.UNKNOWN, 0.0, method="none",
                            note="no history before the target date"), model
    hist = closes[: keep[-1] + 1]
    through = dates[keep[-1]]

    # DEFAULT: rule-based. It is ~15 lines, has no dependencies, nothing to
    # fit and nothing to persist -- and therefore no look-ahead surface at all.
    # The HMM is available for those who want it, but a heavier model is not
    # automatically a better one, and this is the component most likely to be
    # fooling you.
    if engine != "hmm":
        res = rule_based_regime(hist)
        res.as_of = through.isoformat()
        return res, model

    m = model or RegimeModel()
    if not m.is_fitted:
        m.fit(hist, through_date=through)

    if m.is_fitted:
        res = m.infer(hist)
        res.as_of = through.isoformat()
        if res.regime is not Regime.UNKNOWN:
            return res, m

    if fallback == "rule_based":
        res = rule_based_regime(hist)
        res.as_of = through.isoformat()
        res.note = (res.note + " (HMM unavailable: " + (m.fit_note or "?") + ")").strip()
        return res, m
    if fallback == "allow":
        return RegimeResult(Regime.UNKNOWN, 0.0, method="disabled",
                            as_of=through.isoformat(),
                            note="regime gate disabled by config"), m
    return RegimeResult(Regime.CHOP, 0.0, method="blocked", as_of=through.isoformat(),
                        note="regime unavailable and fallback=block"), m
