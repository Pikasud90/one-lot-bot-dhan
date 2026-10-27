"""Post-trade Guardian: hold confidence, trailing stop, and exit triggers.

Keeps v1's genuinely good idea — cheap gates for entry latency, expensive
analytics only once capital is at risk — and fixes what it was built on:

  B3  VWAP and volume divergence run on the FUTURES. The Bank Nifty index has
      no traded volume, so v1's "institutional anchor" was 0/0 and its
      "tick-volume drops 20%" test had no volume to measure.
  A6  The trailing stop now has something to trail TOWARD. A fixed target
      exists, the trail activates only after +1R, and it RATCHETS — it can
      never move against you.
  A7  A time stop exists. A position going sideways is not flat; it is
      bleeding theta.
  A3  EOD square-off escalates and cannot be ignored.
  A2  Exit levels are tested against the BID, because that is where you sell.

Hold confidence is ADVISORY. It colours the dashboard; it never auto-exits.
The stops are what protect capital.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import numpy as np

from ..analytics.indicators import adx, atr, has_tradeable_volume, is_rising, rsi, vwap
from ..costs import Quote, round_to_tick
from .state import ExitReason, Position

log = logging.getLogger(__name__)


class HoldConfidence(str):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass
class GuardianDecision:
    action: str                       # HOLD | EXIT | WARN
    reason: ExitReason | None = None
    message: str = ""
    hold_confidence: str = HoldConfidence.UNKNOWN
    trail_stop: float | None = None
    trail_active: bool = False
    high_water: float | None = None
    unrealised_r: float = 0.0
    unrealised_net: float = 0.0
    details: dict = field(default_factory=dict)

    @property
    def is_exit(self) -> bool:
        return self.action == "EXIT"

    def as_dict(self) -> dict:
        return {"action": self.action,
                "reason": self.reason.value if self.reason else None,
                "message": self.message, "hold_confidence": self.hold_confidence,
                "trail_stop": self.trail_stop, "trail_active": self.trail_active,
                "high_water": self.high_water,
                "unrealised_r": round(self.unrealised_r, 3),
                "unrealised_net": round(self.unrealised_net, 2),
                "details": dict(self.details)}


@dataclass
class SlippageCheck:
    within_tolerance: bool
    signalled: float
    live: float
    drift: float
    tolerance: float
    message: str

    def as_dict(self) -> dict:
        return {"within_tolerance": self.within_tolerance,
                "signalled": self.signalled, "live": self.live,
                "drift": round(self.drift, 2), "tolerance": round(self.tolerance, 2),
                "message": self.message}


def check_entry_slippage(signalled_ask: float, live_quote: Quote,
                         pct_tolerance: float, spread_mult: float) -> SlippageCheck:
    """Compare the live ask against the signalled ask.

    v1 compared LTP to LTP with a flat 3% band. Two corrections:
      * a BUY fills at the ASK, so that is what must be compared;
      * a pure-percentage band is wrong at both ends — 3% of a Rs 300 option is
        Rs 9, narrower than the natural spread on some ITM strikes, so valid
        fills get rejected. Tolerance is max(pct, k x spread).

    A breach is a WARNING, not a veto. If the human has already filled, the
    system must track reality rather than refuse to believe it.
    """
    live_ask = live_quote.ask or 0.0
    drift = live_ask - signalled_ask
    spread = live_quote.spread if np.isfinite(live_quote.spread) else 0.0
    tolerance = max(pct_tolerance * signalled_ask, spread_mult * spread)
    ok = abs(drift) <= tolerance
    if ok:
        msg = f"fill within tolerance (drift Rs {drift:+.2f}, allowed Rs {tolerance:.2f})"
    else:
        msg = (f"price moved Rs {drift:+.2f} from the signalled ask "
               f"{signalled_ask:.2f} (tolerance Rs {tolerance:.2f}) — "
               "confirm you still want this trade")
    return SlippageCheck(ok, signalled_ask, live_ask, drift, tolerance, msg)


@dataclass
class GuardianInputs:
    """Everything the Guardian needs for one evaluation."""

    now: dt.datetime
    quote: Quote                       # the OPTION's live two-sided market
    index_high: np.ndarray
    index_low: np.ndarray
    index_close: np.ndarray
    futures_high: np.ndarray = field(default_factory=lambda: np.array([]))
    futures_low: np.ndarray = field(default_factory=lambda: np.array([]))
    futures_close: np.ndarray = field(default_factory=lambda: np.array([]))
    futures_volume: np.ndarray = field(default_factory=lambda: np.array([]))
    option_volume: float | None = None
    session_window: object | None = None
    regime_direction: str | None = None       # "CE"/"PE"/"CHOP"
    data_degraded: bool = False


class Guardian:
    def __init__(self, cfg, cost_model):
        self.cfg = cfg
        self.cost_model = cost_model

    # ── hold confidence (advisory) ────────────────────────────────────────
    def _hold_confidence(self, pos: Position, inp: GuardianInputs) -> tuple[str, dict]:
        g = self.cfg.strategy.guardian
        details: dict = {}

        # VWAP on the FUTURES — the index has no volume (defect B3).
        vwap_ok: bool | None = None
        if has_tradeable_volume(inp.futures_volume) and len(inp.futures_close) > 2:
            v = vwap(inp.futures_high, inp.futures_low, inp.futures_close,
                     inp.futures_volume)
            if len(v) and not np.isnan(v[-1]):
                px = float(inp.futures_close[-1])
                details["vwap"] = round(float(v[-1]), 2)
                details["futures"] = round(px, 2)
                vwap_ok = px > v[-1] if pos.direction == "CE" else px < v[-1]
                details["vwap_supports"] = vwap_ok
        else:
            details["vwap"] = None
            details["vwap_note"] = ("no traded volume available — VWAP requires the "
                                    "futures series, not the index")

        adx_ok: bool | None = None
        if len(inp.index_close) > g.adx_period * 2:
            a, _, _ = adx(inp.index_high, inp.index_low, inp.index_close, g.adx_period)
            if not np.isnan(a[-1]):
                details["adx"] = round(float(a[-1]), 2)
                details["adx_rising"] = is_rising(a, 3)
                adx_ok = a[-1] >= g.adx_hold_min

        votes = [x for x in (vwap_ok, adx_ok) if x is not None]
        if not votes:
            return HoldConfidence.UNKNOWN, details
        if all(votes):
            return HoldConfidence.HIGH, details
        if any(votes):
            return HoldConfidence.MEDIUM, details
        return HoldConfidence.LOW, details

    # ── divergence (defect B3: needs real volume) ─────────────────────────
    def _divergence(self, pos: Position, inp: GuardianInputs) -> tuple[bool, str]:
        g = self.cfg.strategy.guardian
        if not has_tradeable_volume(inp.futures_volume) or len(inp.futures_close) < 25:
            return False, "divergence check unavailable (no futures volume)"

        closes = inp.index_close
        if len(closes) < 25:
            return False, "insufficient bars"

        lookback = 10
        recent, prior = closes[-lookback:], closes[-2 * lookback:-lookback]
        made_extreme = (np.max(recent) > np.max(prior) if pos.direction == "CE"
                        else np.min(recent) < np.min(prior))
        if not made_extreme:
            return False, "no new extreme"

        vol_recent = float(np.nanmean(inp.futures_volume[-lookback:]))
        vol_prior = float(np.nanmean(inp.futures_volume[-2 * lookback:-lookback]))
        if vol_prior <= 0:
            return False, "no prior volume baseline"
        vol_drop = (vol_prior - vol_recent) / vol_prior

        r = rsi(closes, self.cfg.strategy.momentum.rsi_period)
        if np.isnan(r[-1]) or np.isnan(r[-1 - lookback]):
            return False, "RSI undefined"
        rsi_diverges = (r[-1] < r[-1 - lookback] if pos.direction == "CE"
                        else r[-1] > r[-1 - lookback])

        if vol_drop >= g.divergence_volume_drop_pct and rsi_diverges:
            return True, (f"new extreme on {vol_drop:.0%} lower futures volume with "
                          f"RSI diverging ({r[-1 - lookback]:.1f} -> {r[-1]:.1f})")
        return False, f"volume drop {vol_drop:.0%}, RSI diverging={rsi_diverges}"

    # ── main evaluation ───────────────────────────────────────────────────
    def evaluate(self, pos: Position, inp: GuardianInputs) -> GuardianDecision:
        e = self.cfg.strategy.exits
        tick = self.cfg.instrument.tick_size

        # You exit a long by hitting the BID. Using LTP here would show stops
        # holding that have in fact already been breached (defect A2).
        exit_px = inp.quote.bid if (inp.quote.bid or 0) > 0 else (inp.quote.ltp or 0.0)
        mark = inp.quote.mid if inp.quote.is_valid else exit_px

        high_water = max(pos.high_water_premium or pos.entry_price, mark)
        unreal_r = pos.unrealised_r(exit_px)
        unreal_net = self.cost_model.net_pnl(pos.entry_price, exit_px, pos.qty)

        conf, details = self._hold_confidence(pos, inp)
        details["exit_price_basis"] = "bid" if (inp.quote.bid or 0) > 0 else "ltp"

        # ── trailing stop: activates after +1R, RATCHETS ONLY UP ──────────
        atr_arr = atr(inp.index_high, inp.index_low, inp.index_close, e.atr_period)
        atr_val = float(atr_arr[-1]) if len(atr_arr) and not np.isnan(atr_arr[-1]) else None
        trail_active = pos.trail_active
        trail_stop = pos.trail_stop

        if unreal_r >= e.trail_activate_r:
            trail_active = True
        if trail_active and atr_val and atr_val > 0:
            delta_abs = abs(pos.payload.get("delta", 0.0)) or 0.65
            candidate = round_to_tick(mark - e.sl_atr_mult * atr_val * delta_abs,
                                      tick, "down")
            # THE ratchet. A trailing stop that can fall is not a stop.
            trail_stop = candidate if trail_stop is None else max(trail_stop, candidate)
            trail_stop = max(trail_stop, pos.initial_stop)
        details["atr"] = round(atr_val, 2) if atr_val else None

        effective_stop = pos.initial_stop
        if trail_active and trail_stop is not None:
            effective_stop = max(trail_stop, pos.initial_stop)
        details["effective_stop"] = round(effective_stop, 2)

        base = {"hold_confidence": conf, "trail_stop": trail_stop,
                "trail_active": trail_active, "high_water": high_water,
                "unrealised_r": unreal_r, "unrealised_net": unreal_net,
                "details": details}

        # ── exit triggers, in priority order ──────────────────────────────
        win = inp.session_window
        if win is not None and win.past_squareoff(inp.now):
            return GuardianDecision(
                "EXIT", ExitReason.EOD_SQUAREOFF,
                f"MANDATORY SQUARE-OFF ({win.mandatory_squareoff_at.strftime('%H:%M')}) "
                "— close this position now. Holding to expiry forfeits time value "
                "and hands your exit price to the settlement average.", **base)

        if exit_px <= effective_stop:
            reason = (ExitReason.TRAIL_SL if trail_active and trail_stop is not None
                      and trail_stop > pos.initial_stop else ExitReason.HARD_SL)
            return GuardianDecision(
                "EXIT", reason,
                f"STOP HIT — bid {exit_px:.2f} at or below {effective_stop:.2f} "
                f"({reason.value}). Net Rs {unreal_net:+,.0f}.", **base)

        if exit_px >= pos.target:
            return GuardianDecision(
                "EXIT", ExitReason.TARGET,
                f"TARGET REACHED — bid {exit_px:.2f} at or above {pos.target:.2f}. "
                f"Net Rs {unreal_net:+,.0f}.", **base)

        held = pos.hold_minutes(inp.now)
        details["hold_minutes"] = round(held, 1)
        if held >= e.time_stop_minutes and unreal_r < e.trail_activate_r:
            return GuardianDecision(
                "EXIT", ExitReason.TIME_STOP,
                f"TIME STOP — {held:.0f} minutes without reaching +1R "
                f"(currently {unreal_r:+.2f}R). Theta is winning; close it.", **base)

        # `regime_direction` carries a REGIME label (BULL/BEAR/CHOP), while
        # `pos.direction` carries an OPTION side (CE/PE). Comparing them
        # directly is always unequal, which made every position exit as
        # REGIME_FLIP on its first tick. Map explicitly instead.
        #
        # Only a DEFINITE OPPOSITE regime is an exit. CHOP is a weakening
        # signal, not a reversal, and is handled by hold confidence.
        #
        # Note: the regime is computed DAILY (as of T-1), so for an intraday
        # position this rarely changes mid-trade. It is a slow guard that
        # matters if a position is carried, not a fast one.
        opposite_regime = {"CE": "BEAR", "PE": "BULL"}.get(pos.direction)
        details["regime"] = inp.regime_direction
        if opposite_regime and inp.regime_direction == opposite_regime:
            return GuardianDecision(
                "EXIT", ExitReason.REGIME_FLIP,
                f"REGIME FLIPPED to {inp.regime_direction} against a "
                f"{pos.direction} position. Net Rs {unreal_net:+,.0f}.", **base)

        diverging, why = self._divergence(pos, inp)
        details["divergence"] = why
        if diverging:
            return GuardianDecision(
                "EXIT", ExitReason.DIVERGENCE,
                f"MOMENTUM DIVERGENCE — {why}. Net Rs {unreal_net:+,.0f}.", **base)

        # ── advisory warnings ─────────────────────────────────────────────
        if win is not None and inp.now >= win.eod_advisory_at:
            return GuardianDecision(
                "WARN", None,
                f"Square-off is at {win.mandatory_squareoff_at.strftime('%H:%M')} "
                f"— plan your exit. Net Rs {unreal_net:+,.0f}.", **base)

        if inp.data_degraded:
            return GuardianDecision(
                "WARN", None,
                "Data feed is DEGRADED — the Guardian is running on stale or "
                "fallback prices. Verify against your broker terminal.", **base)

        if conf == HoldConfidence.LOW:
            return GuardianDecision(
                "WARN", None,
                f"Hold confidence LOW ({details.get('adx', 'n/a')} ADX, VWAP "
                f"support {details.get('vwap_supports')}). Stops unchanged.", **base)

        return GuardianDecision(
            "HOLD", None,
            f"{unreal_r:+.2f}R, net Rs {unreal_net:+,.0f}, stop {effective_stop:.2f}",
            **base)
