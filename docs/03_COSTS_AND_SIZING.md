# Costs and Sizing

The original blueprint modelled roughly 40% of real friction. It under-counted
by 3–4×, which is the difference between a signal that clears a 2.0 net R/R
and one that does not.

## Charges, per leg

Given premium `P`, quantity `Q`, side `buy|sell`, `turnover = P × Q`:

| Charge | Rate | Side |
|---|---|---|
| Brokerage | ₹20 per executed order (Dhan F&O) | both |
| STT | 0.10% of premium turnover | **sell only** |
| Exchange transaction | 0.03503% of premium turnover | both |
| SEBI turnover fee | ₹10 per crore (0.0001%) | both |
| NSE IPFT | ₹50 per crore (0.0005%) | both |
| Stamp duty | 0.003% of premium turnover | **buy only** |
| **GST @ 18%** | on brokerage + exchange + SEBI + IPFT — **never** on STT or stamp duty | both |

Rates live in `config/costs.yaml` with an `effective_from` date. **Verify them
against a real contract note** before trusting any net figure — see
[05_COST_RECONCILIATION.md](05_COST_RECONCILIATION.md).

## Worked example

BANKNIFTY 54000 CE, buy 300 / sell 400, 2 lots (Q = 70):

```
BUY @ 300, turnover ₹21,000
  brokerage        20.000000
  STT               0.000000   (never on a purchase)
  exchange txn      7.356300   0.03503% × 21,000
  SEBI fee          0.021000
  IPFT              0.105000
  stamp duty        0.630000   0.003% × 21,000
  GST               4.946814   18% × (20 + 7.3563 + 0.021 + 0.105)
  ─────────────────────────
  TOTAL            33.059114

SELL @ 400, turnover ₹28,000
  brokerage        20.000000
  STT              28.000000   0.10% × 28,000
  exchange txn      9.808400
  SEBI fee          0.028000
  IPFT              0.140000
  stamp duty        0.000000   (buy side only)
  GST               5.395752
  ─────────────────────────
  TOTAL            63.372152

gross P&L      ₹7,000.00
friction          ₹96.43
NET P&L        ₹6,903.57
breakeven        ₹301.25
```

The original model would have said ₹68.00. Add a ₹2.00 spread crossed twice
(₹140) and true friction is **₹236.43 — 3.48× the modelled figure.**

These numbers are pinned by `tests/test_costs.py`. If a rate changes, those
tests **should** fail. Update this document first, then the tests.

## Execution-effective pricing

You do not transact at LTP.

```
entry_eff      = round_up(ask + slippage_ticks × tick)
exit_eff(L)    = round_down(L − slippage_ticks × tick)
```

Entry rounds **up** and exit rounds **down**. Rounding the other way would
manufacture edge the exchange will not give you.

## The numbers on the alert

```
net_pnl(x)   = (x − entry_eff) × Q − leg_cost(entry_eff, buy) − leg_cost(x, sell)
breakeven    = the x where net_pnl(x) = 0        ← shown on every alert
net_risk     = −net_pnl(stop_exit_eff)
net_reward   =  net_pnl(target_exit_eff)
rr_net       = net_reward / net_risk             ← the number that gates the signal
```

Breakeven is solved by bisection, exact to the tick, and rounded **up** — you
cannot break even at a price you cannot quote.

## Sizing: there isn't any

**One lot, always, whatever it costs.** That is the entire model.

```
qty    = 1 x lot_size            (35)
risk   = 1.5 x ATR x |delta| x 35   (+ round-trip costs)
outlay = entry_eff x 35
```

Nothing is rejected for being expensive. The only rejection left is a contract
with no two-sided market, which is a data problem rather than a risk decision.

Worked at today's Bank Nifty (57,762), IV 14%, 15 days, ATR(5m) 90:

| Delta | Strike | Premium | Outlay | Risk at stop | Reward at target | Net R/R |
|---|---|---|---|---|---|---|
| 0.70 | 56,900 | ₹1,171 | ₹41,184 | ₹3,465 | ₹8,217 | 2.37 |
| 0.65 | 57,200 | ₹971 | ₹34,176 | ₹3,138 | ₹7,443 | 2.37 |
| 0.60 | 57,400 | ₹850 | ₹29,909 | ₹2,910 | ₹6,897 | 2.37 |

Risk-per-trade is an **output** of the market (ATR) and the contract (delta).
It does not depend on your account, and your account does not change it.

### Where risk is controlled instead

1. **The stop.** Extreme volatility — which is what makes a stop wide in
   rupees — is excluded upstream by the intraday chop filter's
   `atr_percentile_max`.
2. **Absolute rupee limits.** `daily_loss_limit_inr` and `max_drawdown_inr`.

At the shipped limits (₹6,000 daily, ₹18,000 drawdown) against a typical
₹3,138 one-lot risk: **2 losses breach the day**, **6 breach the drawdown
halt**. `python -m onelot.cost_preview` recomputes this for any market level.

### Concentration

One lot of a 0.65-delta option is ₹34,176 of premium. Whether that is a large
share of your account is a question only you can answer, which is why
`account.capital_inr` is optional — set it and the dashboard will warn above
`deployment_warn_pct`; leave it at 0 and the system simply does not ask.
