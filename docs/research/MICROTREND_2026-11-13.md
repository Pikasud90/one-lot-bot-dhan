# The owner's options rule of 13 November 2026: a backtest

On 13 November 2026 the owner fixed one rule for index options and asked for nothing else to
be tracked. This note records what that exact rule did on five years of bars. It is
backtest evidence, not advice.

## The rule

- Supertrend(14, 1.7) on closed bars of 5m, 10m, 15m, 30m, 60m and 4h (the micro trend) and
  of 3m (the trigger). A 4h bar runs from 09:15 to 13:15 and from 13:15 to the close.
- Entry at a 3-minute close where all seven point the same way and did not at the previous
  3-minute close: a call when they point up, a put when they point down. Entries from 09:30
  to 14:45, one trade at a time in an index.
- Exit at the first 3-minute close where at least 3 of the 7 point against the trade, or at
  the session's option exit (15:15). No target and no other stop.

## The result

Five indices, 2021-11-08 to 2026-11-06, one lot, modelled premiums (Black-76 at India VIX ×
1.15, monthly contract, the product's spread and charges). A t-value treats all trades of one
day as one draw. "Index" is the move of the index in the trade's favour.

| Cut | Trades | % of premium a trade (t) | Index, bp | Winners |
|---|---|---|---|---|
| All | 3,824 | −0.24 (t −0.6) | +3.1 | 38.3% |
| TUNE | 2,267 | −0.18 (t −0.4) | +3.3 | 39.1% |
| CHOOSE | 802 | +0.44 (t 0.5) | +4.4 | 38.4% |
| TEST | 755 | −1.15 (t −1.4) | +1.4 | 36.0% |
| Calls | 2,319 | −0.52 (t −1.3) | +2.5 | 38.0% |
| Puts | 1,505 | +0.19 (t 0.3) | +4.2 | 38.9% |
| Ended by 3 of 7 reversing | 2,188 | −6.56 (t −19.8) | −16.7 | 15.7% |
| Ended at the session's exit | 1,636 | +8.21 (t 13.0) | +29.7 | 68.6% |

By index: BANKNIFTY −0.39% (728 trades), NIFTY −0.19% (790), FINNIFTY −0.50% (733),
MIDCPNIFTY +0.30% (759), SENSEX −0.43% (814). About 765 trades a year over the five
indices, held 140 minutes on average.

## What it says

- The rule is right about the direction a little more often than not: +3.1 bp of index a
  trade. An option buyer needs 5 to 10 bp to cover the spread, the charges and the time
  value (ALPHA_SEARCH_2026-11-12.md, section 4). So the result after costs is about zero,
  and negative in the last year.
- Trades that reach the session's exit make money and trades that end on the reversal count
  lose it; the two cancel. This is the same picture as every same-day trend rule tested
  before (STRATEGIES_TESTED_2026-11-12.md).
- These are modelled premiums. On real premiums the earlier same-day trend rules came out 1
  to 2 points lower, and so did this rule when it was priced the same day: about −1.2% of
  premium a trade on NIFTY and BANKNIFTY (MICROTREND_STUDY_2026-11-13.md).

**Verdict.** Not an edge. The rule is in the paper test because the owner chose it as the
one way to follow options; every alert of the rule says what this backtest found.

Reproduce (read-only on the store, about 30 seconds):

```bash
cd "/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN"
PYTHONPATH=src:scripts:. .venv/bin/python -W ignore scripts/microtrend_backtest.py
```
