"""One-unit economics for a desk signal, and the alert text the operator acts on.

STOCK  : 1 share. Entry/stop/target are share prices; charges from the equity_intraday
         cost table.
INDEX  : 1 lot of a delta-selected option (CE for bullish, PE for bearish), priced with
         the same Black-76 model as the Backtest Lab off the current underlying and
         India VIX. Stop/target are underlying levels; the premium at those levels gives
         the rupee risk/reward. Labelled SYNTHETIC until the Data-API option chain is wired.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..clock import IST
from ..config import BacktestCfg, CostConfig
from ..costs import CostModel
from ..data.universe import InstrumentSpec
from ..lab.execution import (
    OptionPricer,
    expiry_datetime,
    monthly_expiry,
    strike_for_delta,
    year_frac,
)
from ..settings import current_settings


@dataclass
class Economics:
    kind: str  # STOCK | INDEX
    side: str  # BUY | SELL | BUY_CE | BUY_PE
    qty: int
    unit_label: str  # "1 share" | "1 lot (30)"
    underlying_entry: float
    underlying_stop: float
    underlying_target: float | None
    entry_price: float  # per unit: share price or option premium
    stop_price: float
    target_price: float | None
    outlay: float
    risk_inr: float
    reward_inr: float | None
    costs_inr: float
    rr_net: float | None
    r_target: float
    option: dict = field(default_factory=dict)
    synthetic: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


# costs.yaml parsed once per process (US-023 AC4), keyed by its path so a settings reload onto
# another project root reads that project's table; one CostModel per (path, cost product).
_cost_tables: dict[Path, CostConfig] = {}
_cost_models: dict[tuple[Path, str], CostModel] = {}
_cost_lock = threading.Lock()


def _costs_for(spec: InstrumentSpec) -> CostModel:
    """The CostModel of `spec.product`. The table is parsed on the first call only: under the
    lock, so racing threads never parse it twice. CostModel is a stateless calculator, so
    one shared instance per product is safe."""
    path = Path(current_settings().project_root) / "config" / "costs.yaml"
    key = (path, str(spec.product))
    with _cost_lock:
        model = _cost_models.get(key)
        if model is None:
            table = _cost_tables.get(path)
            if table is None:
                table = _cost_tables[path] = CostConfig.load(path)
            model = _cost_models[key] = CostModel(table.product(spec.product))
    return model


def _clear_cost_cache() -> None:
    """Forget the parsed cost tables (tests; a later call parses the file again)."""
    with _cost_lock:
        _cost_tables.clear()
        _cost_models.clear()


def stock_economics(
    spec: InstrumentSpec, direction: str, entry: float, stop: float, target_r: float
) -> Economics:
    sign = 1 if direction == "long" else -1
    risk_pts = abs(entry - stop)
    target = entry + sign * target_r * risk_pts if target_r > 0 else None
    costs = _costs_for(spec)
    exit_for_cost = target if target else entry
    c = costs.round_trip_cost(entry, exit_for_cost, 1)
    reward = (abs(target - entry) - c) if target else None
    return Economics(
        "STOCK",
        "BUY" if sign > 0 else "SELL",
        1,
        "1 share",
        entry,
        stop,
        target,
        entry,
        stop,
        target,
        entry,
        risk_pts + c,
        reward,
        c,
        (reward / (risk_pts + c)) if reward is not None and risk_pts + c > 0 else None,
        target_r,
    )


def index_option_economics(
    spec: InstrumentSpec,
    direction: str,
    entry: float,
    stop: float,
    target_r: float,
    now: dt.datetime,
    vix: float | None,
    bt: BacktestCfg | None = None,
) -> Economics:
    bt = bt or current_settings().backtest
    sign = 1 if direction == "long" else -1
    opt = "CE" if sign > 0 else "PE"
    risk_pts = abs(entry - stop)
    target = entry + sign * target_r * risk_pts if target_r > 0 else None
    vix_series = (
        pd.Series([vix], index=[pd.Timestamp(now).tz_convert(IST).normalize()]) if vix else None
    )
    pricer = OptionPricer(bt, vix_series)
    ts = pd.Timestamp(now).tz_convert(IST)
    exp_d = monthly_expiry(ts.date(), bt.option_expiry_weekday, bt.option_min_days_to_expiry)
    expiry = expiry_datetime(exp_d)
    t = year_frac(now, expiry)
    iv = pricer.iv_for(ts)
    k = strike_for_delta(
        entry, t, iv, opt, bt.option_target_delta, spec.strike_step, bt.risk_free_rate
    )
    prem = pricer.premium(entry, k, ts, expiry, opt)
    delta = pricer.delta(entry, k, ts, expiry, opt)
    prem_stop = pricer.premium(stop, k, ts, expiry, opt)
    prem_target = pricer.premium(target, k, ts, expiry, opt) if target else None
    lot = int(spec.lot_size)
    costs = _costs_for(spec)
    c = costs.round_trip_cost(prem, prem_target or prem, lot)
    risk = max(prem - prem_stop, 0.0) * lot + c
    reward = ((prem_target - prem) * lot - c) if prem_target is not None else None
    return Economics(
        "INDEX",
        f"BUY_{opt}",
        lot,
        f"1 lot ({lot})",
        entry,
        stop,
        target,
        round(prem, 2),
        round(prem_stop, 2),
        round(prem_target, 2) if prem_target else None,
        round(round(prem, 2) * lot, 2),
        round(risk, 2),
        round(reward, 2) if reward is not None else None,
        round(c, 2),
        (round(reward / risk, 2) if reward is not None and risk > 0 else None),
        target_r,
        option={
            "strike": k,
            "opt_type": opt,
            "expiry": exp_d.isoformat(),
            "delta": round(abs(delta), 3),
            "iv": round(iv, 4),
            "premium": round(prem, 2),
            "lot": lot,
        },
        synthetic=True,
        notes=[
            "premium is a Black-76 estimate off India VIX; use the live chain quote when you place the order"
        ],
    )


def alert_text(
    label: str,
    series_key: str,
    eco: Economics,
    strategy: str,
    timeframe: str,
    bar_ts: dt.datetime | None,
    exit_time: str,
    reasons: list[str],
) -> str:
    r = lambda v: f"₹{v:,.2f}"  # noqa: E731
    pct = lambda a, b: f"{(a / b - 1) * 100:+.2f}%"  # noqa: E731
    lines: list[str] = []
    if eco.kind == "STOCK":
        verb = "BUY" if eco.side == "BUY" else "SELL (short, intraday)"
        lines.append(f"{verb} 1 share {label} at ~{r(eco.entry_price)} (market)")
        lines.append(
            f"Stop {r(eco.stop_price)} ({pct(eco.stop_price, eco.entry_price)}, risk {r(eco.risk_inr)} incl. charges)"
        )
        if eco.target_price:
            lines.append(
                f"Target {r(eco.target_price)} ({pct(eco.target_price, eco.entry_price)}, {eco.r_target:g}R, "
                f"net reward {r(eco.reward_inr or 0)}; net R/R {eco.rr_net:.2f})"
                if eco.rr_net is not None
                else f"Target {r(eco.target_price)} ({eco.r_target:g}R)"
            )
        else:
            lines.append(
                "No fixed target: trail the stop per the strategy; exit on the opposite signal"
            )
    else:
        o = eco.option
        lines.append(
            f"BUY {eco.unit_label} {series_key} {o['strike']:g} {o['opt_type']} "
            f"(Δ≈{o['delta']:.2f}, est. premium {r(o['premium'])} → outlay {r(eco.outlay)})"
        )
        lines.append(
            f"Underlying {eco.underlying_entry:,.2f} → stop {eco.underlying_stop:,.2f} "
            f"(premium ≈ {r(eco.stop_price)}, risk ≈ {r(eco.risk_inr)})"
        )
        if eco.underlying_target:
            lines.append(
                f"Target {eco.underlying_target:,.2f} (premium ≈ {r(eco.target_price or 0)}, "
                f"reward ≈ {r(eco.reward_inr or 0)}; net R/R {eco.rr_net if eco.rr_net is not None else '–'})"
            )
        lines.append(
            f"Exit = SELL the same option back. Charges ≈ {r(eco.costs_inr)} round trip. {eco.notes[0] if eco.notes else ''}"
        )
    lines.append(
        f"Square off by {exit_time}. Strategy {strategy} {timeframe}"
        + (f", signal bar {bar_ts.astimezone(IST).strftime('%H:%M')}" if bar_ts else "")
        + "."
    )
    if reasons:
        lines.append("Why: " + "; ".join(reasons[:4]))
    return "\n".join(lines)


def index_option_economics_from_chain(
    spec: InstrumentSpec,
    direction: str,
    entry: float,
    stop: float,
    target_r: float,
    chain,
    now: dt.datetime,
    bt: BacktestCfg | None = None,
) -> Economics | None:
    """Same contract as `index_option_economics`, but strike, premium, delta, IV and the
    spread come from a LIVE Dhan option chain. Returns None when the chain is unusable
    (caller falls back to the synthetic model)."""
    bt = bt or current_settings().backtest
    sign = 1 if direction == "long" else -1
    opt = "CE" if sign > 0 else "PE"
    rows = [r for r in getattr(chain, "rows", []) if r.opt_type == opt and r.ltp and r.ltp > 0]
    if not rows:
        return None
    target_delta = bt.option_target_delta
    with_delta = [r for r in rows if r.delta is not None]
    if with_delta:
        best = min(with_delta, key=lambda r: abs(abs(r.delta) - target_delta))
    else:  # no greeks: nearest ITM strike by moneyness
        itm = [r for r in rows if (r.strike <= entry if opt == "CE" else r.strike >= entry)]
        best = min(itm or rows, key=lambda r: abs(r.strike - entry))
    delta = abs(best.delta) if best.delta is not None else target_delta
    iv = best.iv if best.iv else None
    ask = best.ask if best.ask and best.ask > 0 else best.ltp
    bid = best.bid if best.bid and best.bid > 0 else best.ltp
    prem = float(ask)  # you buy at the ask
    spread_pct = ((ask - bid) / ask * 100.0) if ask else None
    risk_pts = abs(entry - stop)
    target = entry + sign * target_r * risk_pts if target_r > 0 else None
    # premium at stop / target: delta-translated (gamma ignored), floored at a tick
    prem_stop = max(0.05, prem - delta * risk_pts)
    prem_target = (prem + delta * target_r * risk_pts) if target else None
    lot = int(spec.lot_size)
    costs = _costs_for(spec)
    c = costs.round_trip_cost(prem, prem_target or prem, lot)
    risk = max(prem - prem_stop, 0.0) * lot + c
    reward = ((prem_target - prem) * lot - c) if prem_target is not None else None
    exp = getattr(chain, "expiry", None)
    return Economics(
        "INDEX",
        f"BUY_{opt}",
        lot,
        f"1 lot ({lot})",
        entry,
        stop,
        target,
        round(prem, 2),
        round(prem_stop, 2),
        round(prem_target, 2) if prem_target else None,
        round(round(prem, 2) * lot, 2),
        round(risk, 2),
        round(reward, 2) if reward is not None else None,
        round(c, 2),
        (round(reward / risk, 2) if reward is not None and risk > 0 else None),
        target_r,
        option={
            "strike": float(best.strike),
            "opt_type": opt,
            "expiry": exp,
            "delta": round(delta, 3),
            "iv": round(float(iv), 4) if iv else None,
            "premium": round(prem, 2),
            "lot": lot,
            "bid": bid,
            "ask": ask,
            "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
            "oi": best.oi,
            "security_id": best.security_id,
        },
        synthetic=False,
        notes=[
            f"live chain quote: bid {bid:.2f} / ask {ask:.2f}"
            + (f" (spread {spread_pct:.1f}%)" if spread_pct is not None else ""),
            "premium at stop/target is delta-translated from the underlying levels",
        ],
    )
