"""US-099: write config/delivery_universe.yaml (the pullback rule's wide list) from the study.

    PYTHONPATH=src .venv/bin/python scripts/write_delivery_universe.py \
        --study "PATH/data/research/swing_2026-11-24" [--out config/delivery_universe.yaml]

Reads the swing-universe study's `universe.csv` (symbol, group, Dhan security id: written by
`scripts/swing_universe.py`, stage `universe`) and its copy of Dhan's instrument master
(`master_nse_eq.csv`) for the company names and the symbols Dhan shows, and the study's trades
of the rule (`trades_P1.pkl`) for the backtest's figure the page shows beside the paper test.
Nothing is fetched: the study's files are read only. The study's groups desk18, alpha43 and new
are written as desk, alpha and new.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {"desk18": "desk", "alpha43": "alpha", "new": "new"}

HEAD = """\
# The pullback rule's wide list (US-099): a PAPER test on {n} stocks. The system never places
# an order. Written by scripts/write_delivery_universe.py from the swing-universe study of
# 24 Nov 2026 (docs/research/SWING_UNIVERSE_2026-11-24.md); edit with care.
# group: desk = one of the desk's 18 stocks (live bars, a card, its own alert near the close);
#        alpha = the alpha study's 43; new = the 161 names nobody had looked at.
# Every stock that is not a desk instrument is DELIVERY-ONLY: daily bars only, no feed, no card.
# segment and security_id: how Dhan names the stock (daily bars through the desk's adapter).
backtest:
  trades: {trades}
  net_pct: {net_pct}
  winners_pct: {winners}
  period: "{first} to {last}"
  note: "The study's trades of this rule on the {n} stocks, after costs. Not proven: about +0.6 of the result a trade is the market's drift, and a book of ten such trades on Rs 1 lakh became Rs 1.30 lakh from 2010 to 2026 against Rs 4.15 lakh for NIFTY simply held (docs/research/PULLBACK_BOOK_2026-11-25.md)."
stocks:
"""


def write(study: Path, out: Path) -> int:
    u = pd.read_csv(study / "universe.csv", dtype=str)
    m = pd.read_csv(study / "master_nse_eq.csv", dtype=str)
    m = m.drop_duplicates("SECURITY_ID").set_index("SECURITY_ID")
    tr = pd.read_pickle(study / "trades_P1.pkl")
    lines = [
        HEAD.format(
            n=len(u),
            trades=len(tr),
            net_pct=round(float(tr.net_pct.mean()), 3),
            winners=round(float((tr.net_pct > 0).mean() * 100), 1),
            first=pd.Timestamp(tr.signal_date.min()).date(),
            last=pd.Timestamp(tr.signal_date.max()).date(),
        )
    ]
    for r in u.itertuples():
        row = m.loc[r.security_id] if r.security_id in m.index else None
        name = str(row["DISPLAY_NAME"]) if row is not None else r.symbol
        symbol = str(row["UNDERLYING_SYMBOL"]) if row is not None else r.symbol
        name = name.replace('"', "'")
        lines.append(
            f'  - {{key: "{r.symbol}", name: "{name}", symbol: "{symbol}", '
            f'security_id: "{r.security_id}", segment: NSE_EQ, group: {GROUPS[r.group]}}}\n'
        )
    out.write_text("".join(lines), encoding="utf-8")
    print(f"{out}: {len(u)} stocks " + str(u.group.map(GROUPS).value_counts().to_dict()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--study", type=Path, required=True, help="the study's folder (read only)")
    ap.add_argument("--out", type=Path, default=ROOT / "config" / "delivery_universe.yaml")
    a = ap.parse_args()
    return write(a.study, a.out)


if __name__ == "__main__":
    raise SystemExit(main())
