# Cost Reconciliation Worksheet

`config/costs.yaml` ships with `verified_against_contract_note: false`, and the
engine warns on every startup until you change it. This is that procedure.

**Why it matters:** every net figure the system shows you — breakeven, net R/R,
position size, journal P&L — is derived from these rates. If they are wrong,
every one of those numbers is wrong in the same direction, and the error is
invisible because it is *consistent*.

## Procedure

1. Take **one real options trade** (a paper-thin 1-lot trade is fine).
2. Download the **contract note** from Dhan for that day.
3. Fill in the table below from the contract note.
4. Compute the same trade with the engine:

```bash
python - <<'PY'
import sys; sys.path.insert(0, "src")
from onelot.config import CostConfig
from onelot.costs import CostModel
cm = CostModel(CostConfig.load("config/costs.yaml"))

PREMIUM_BUY  = 300.00      # <- your actual buy price
PREMIUM_SELL = 310.00      # <- your actual sell price
QTY          = 35          # <- lots x lot size

for side, px in (("buy", PREMIUM_BUY), ("sell", PREMIUM_SELL)):
    b = cm.leg(px, QTY, side)
    print(f"\n{side.upper()} @ {px}  turnover Rs {b.turnover:,.2f}")
    for k in ("brokerage","stt","exchange_txn","sebi_fee","ipft","stamp_duty","gst"):
        print(f"  {k:<14}{getattr(b,k):>12.4f}")
    print(f"  {'TOTAL':<14}{b.total:>12.4f}")
print(f"\nNET P&L: Rs {cm.net_pnl(PREMIUM_BUY, PREMIUM_SELL, QTY):,.2f}")
PY
```

5. Compare, line by line.

## Worksheet

| Line | Contract note | Engine | Match? |
|---|---|---|---|
| Brokerage (buy) | | | |
| Brokerage (sell) | | | |
| STT (sell) | | | |
| Exchange transaction charge | | | |
| SEBI turnover fee | | | |
| IPFT | | | |
| Stamp duty (buy) | | | |
| GST | | | |
| **Total charges** | | | |
| **Net P&L** | | | |

## If a line disagrees

Edit the corresponding rate in `config/costs.yaml`, bump `effective_from`, and
re-run. **Then run `pytest tests/test_costs.py`** — it will fail, because the
reference figures there are pinned to the old rates. Update the expected values
in that file and in `docs/03_COSTS_AND_SIZING.md` so the three stay in sync.

Common causes of disagreement:

- Your brokerage plan is not flat ₹20 → set `brokerage.mode: min_of`.
- Rates changed after `effective_from`. Budgets and exchange circulars move
  them; the file is dated for exactly this reason.
- GST computed on the wrong base — it applies to brokerage + exchange + SEBI +
  IPFT, **never** to STT or stamp duty.

## When it all matches

```yaml
verified_against_contract_note: true
```

The startup warning disappears. Re-verify after any budget announcement or
exchange circular.
