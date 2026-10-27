"""`python -m onelot.cost_preview` — what will one lot cost me?

With the one-lot rule there is no sizing decision and nothing is ever rejected
for being expensive, so this is not a gate check. It answers the practical
question before the open: at today's levels, what does one lot of each delta
cost in premium, and what does the stop put at risk in rupees?

No credentials, no network — prices with Black-76.
"""

from __future__ import annotations

import argparse
import math
import sys

import numpy as np

from .analytics.blackscholes import greeks
from .config import AppConfig, CostConfig
from .costs import CostModel, Quote
from .strategy.sizer import size_position


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m onelot.cost_preview")
    p.add_argument("--spot", type=float, default=52_000.0,
                   help="BankNifty futures level")
    p.add_argument("--iv", type=float, default=0.14, help="ATM IV")
    p.add_argument("--days", type=float, default=15.0, help="days to expiry")
    p.add_argument("--atr", type=float, default=80.0, help="5-min ATR, index points")
    p.add_argument("--config", default=None)
    args = p.parse_args(argv)

    cfg = AppConfig.load(args.config)
    cm = CostModel(CostConfig.load(cfg.project_root / "config" / "costs.yaml"))
    r = cfg.risk
    lot = cfg.instrument.lot_size_override or 35
    cap = cfg.account.capital_inr
    T = args.days / 365.0
    cc = cfg.strategy.contract

    print("=" * 74)
    print("  ONE-LOT COST PREVIEW")
    print("=" * 74)
    print(f"  BankNifty {args.spot:,.0f}   IV {args.iv:.1%}   {args.days:g}d   "
          f"ATR(5m) {args.atr:.0f}   lot {lot}")
    print(f"  rule: {r.lots_per_trade} lot per trade, always")
    print(f"  daily loss limit Rs {r.daily_loss_limit_inr:,.0f}   "
          f"drawdown limit Rs {r.max_drawdown_inr:,.0f}")
    print(f"  delta band {cc.delta_min:.2f}-{cc.delta_max:.2f}")
    print("=" * 74)

    ladder = []
    for k in np.arange(args.spot * 0.85, args.spot * 1.15, 100.0):
        g = greeks(args.spot, float(k), T, args.iv, 0.0, "CE")
        if g.price > 0.05:
            ladder.append((float(k), g.delta, g.price))
    if not ladder:
        print("  could not build a strike ladder — check the inputs")
        return 1

    print(f"\n{'delta':>7}{'strike':>9}{'premium':>10}{'outlay':>11}"
          f"{'risk at stop':>14}{'target':>11}{'R/R':>7}")
    for target in (0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40, 0.35):
        k, d, px = min(ladder, key=lambda row: abs(row[1] - target))
        q = Quote(bid=round(px * 0.995, 2), ask=round(px * 1.005, 2), ltp=px)
        res = size_position(
            quote=q, stop_distance=cfg.strategy.exits.sl_atr_mult * args.atr * abs(d),
            rr_target_gross=cfg.strategy.exits.rr_target_gross, lot_size=lot,
            risk_cfg=r, cost_model=cm, exec_cfg=cfg.execution_assumptions,
            tick=cfg.instrument.tick_size, capital=cap)
        e = res.economics
        mark = " *" if cc.delta_min <= target <= cc.delta_max else "  "
        print(f"{target:>7.2f}{k:>9,.0f}{px:>10.2f}{e.premium_outlay:>11,.0f}"
              f"{e.net_risk:>14,.0f}{e.net_reward:>11,.0f}{e.rr_net:>7.2f}{mark}")
    print("\n  * = inside your configured delta band")

    k, d, px = min(ladder, key=lambda row: abs(row[1] - (cc.delta_min + cc.delta_max) / 2))
    q = Quote(bid=round(px * 0.995, 2), ask=round(px * 1.005, 2), ltp=px)
    res = size_position(
        quote=q, stop_distance=cfg.strategy.exits.sl_atr_mult * args.atr * abs(d),
        rr_target_gross=cfg.strategy.exits.rr_target_gross, lot_size=lot,
        risk_cfg=r, cost_model=cm, exec_cfg=cfg.execution_assumptions,
        tick=cfg.instrument.tick_size, capital=cap)
    e = res.economics

    print("\n" + "=" * 74)
    print("  TYPICAL TRADE AT THE MIDDLE OF YOUR BAND")
    print("=" * 74)
    print(f"  premium outlay        Rs {e.premium_outlay:>10,.0f}")
    print(f"  risk if stopped       Rs {e.net_risk:>10,.0f}")
    print(f"  reward if target hit  Rs {e.net_reward:>10,.0f}   (net R/R {e.rr_net:.2f})")
    print(f"  breakeven premium     Rs {e.breakeven:>10.2f}")
    # Number of losses that BREACH the limit, not the number it absorbs.
    # int(6000 // 3138) = 1 reads as "one loss stops you", which is wrong:
    # one loss leaves you at -3,138, still inside a 6,000 limit.
    risk = max(e.net_risk, 1.0)
    day_breach = math.ceil(r.daily_loss_limit_inr / risk)
    dd_breach = math.ceil(r.max_drawdown_inr / risk)
    print(f"\n  losses that BREACH the daily limit       {day_breach:>3}"
          f"   (capped at {r.max_trades_per_day} trades/day, so worst day is "
          f"Rs {min(r.max_trades_per_day, day_breach) * risk:,.0f})")
    print(f"  losses that BREACH the drawdown halt     {dd_breach:>3}")
    if cap > 0 and res.concentration_pct:
        print(f"\n  CONCENTRATION: Rs {e.premium_outlay:,.0f} is "
              f"{res.concentration_pct:.0%} of the capital you declared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
