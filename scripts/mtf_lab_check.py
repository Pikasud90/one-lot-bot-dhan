"""The registry strategy `supertrend_mtf` through the Lab engine (the product path).

The research harness (`mtf_bruteforce.py`, `mtf_stage2.py`) decides every minute and lets trades
overlap. The product decides at the close of each 5-minute bar, holds one position per symbol
and ends every trade at the session close. This script runs the product path on five years and
prints it beside the harness numbers, so the two can be compared, and it is how the defaults of
the strategy (whose line is the trail, how far the backstop stands) were chosen.

    .venv/bin/python scripts/mtf_lab_check.py --run [--workers 5] [--only INDEX_OPTIONS]
    .venv/bin/python scripts/mtf_lab_check.py --report

Evidence about rules on past data, after modelled costs; not advice, not a forecast.
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deep_backtest_candidates as d  # noqa: E402

OUT = d.ROOT / "data" / "research" / "mtf_2026-11-11"
STORE = OUT / "lab"
DEST = OUT / "mtf_lab_check.txt"

OWNER = {"gate": "all", "trend_tf": "30m", "reenter_daily": True, "entry_end": "14:45"}
VOLC = {
    "gate": "all", "trend_tf": "30m", "flip_window": 4, "trend_adx_min": 25.0,
    "trend_rsi_min": 60.0, "trend_rvol_min": 1.5, "entry_end": "13:00", "max_trades_per_day": 1,
}  # fmt: skip
CALLS = {"allow_long": True, "allow_short": False}
PUTS = {"allow_long": False, "allow_short": True}

#: basket -> [(variant, params, harness figure it is closest to)]
VARIANTS: dict[str, list[tuple[str, dict, str]]] = {
    "INDEX_OPTIONS": [
        ("owner rule, 30m line, touch", {**OWNER, **CALLS, "stop_buffer_atr": 0.0},
         "harness same day, 30m touch: about -0.2 to -0.5"),
        ("owner rule, 30m line, close + 1 ATR", {**OWNER, **CALLS, "stop_buffer_atr": 1.0},
         "harness owner_intra (30m close): -0.541"),
        ("owner rule, 60m line, close + 1 ATR",
         {**OWNER, **CALLS, "trail_tf": "60m", "stop_buffer_atr": 1.0}, ""),
        ("owner rule, puts, 30m line, close + 1 ATR", {**OWNER, **PUTS, "stop_buffer_atr": 1.0}, ""),
        ("fresh turn + ADX/RSI, 60m line, touch",
         {**VOLC, **CALLS, "trail_tf": "60m", "stop_buffer_atr": 0.0}, "harness 60m touch: +1.181"),
        ("fresh turn + ADX/RSI, 60m line, close + 1 ATR",
         {**VOLC, **CALLS, "trail_tf": "60m", "stop_buffer_atr": 1.0}, "harness 60m close: +2.779"),
        ("fresh turn + ADX/RSI, 60m line, close + 2 ATR",
         {**VOLC, **CALLS, "trail_tf": "60m", "stop_buffer_atr": 2.0}, "harness 60m close: +2.779"),
        ("fresh turn + ADX/RSI, 60m line, close + 3 ATR",
         {**VOLC, **CALLS, "trail_tf": "60m", "stop_buffer_atr": 3.0}, "harness 60m close: +2.779"),
        ("fresh turn + ADX/RSI, 30m line, close + 1 ATR",
         {**VOLC, **CALLS, "stop_buffer_atr": 1.0}, "harness 30m close: +0.850"),
        ("fresh turn + ADX/RSI, 1d line, close + 1 ATR",
         {**VOLC, **CALLS, "trail_tf": "1d", "stop_buffer_atr": 1.0}, "harness intra_volc: +3.066"),
    ],
    "STOCKS": [
        ("owner rule, long, 30m line, close + 1 ATR", {**OWNER, **CALLS, "stop_buffer_atr": 1.0},
         "harness owner_long: -0.131"),
        ("owner rule, short, 30m line, close + 1 ATR", {**OWNER, **PUTS, "stop_buffer_atr": 1.0},
         "harness owner_short: -0.157"),
    ],
}  # fmt: skip

BASKETS = {"INDEX_OPTIONS": d.INDEX_OPTIONS, "STOCKS": d.STOCKS}


def run(workers: int, only: list[str] | None) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    tasks = []
    for basket, variants in VARIANTS.items():
        if only and basket not in only:
            continue
        specs = [
            (name, {"timeframe": "5m", "params": params, "fine": True, "cost": 1.0})
            for name, params, _ in variants
        ]
        tasks += [(basket, "supertrend_mtf", inst, specs) for inst in BASKETS[basket]]
    print(f"{len(tasks)} instrument tasks, {sum(len(t[3]) for t in tasks)} backtests", flush=True)
    results: dict[str, dict[str, dict]] = {b: {} for b in VARIANTS}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=d._init_worker) as ex:  # noqa: SLF001
        futs = [ex.submit(d._run_instrument, t) for t in tasks]  # noqa: SLF001
        for n, fut in enumerate(as_completed(futs), 1):
            basket, inst, out, secs = fut.result()
            results[basket][inst] = out
            errs = {k: v for k, v in out.items() if isinstance(v, str)}
            print(
                f"[{n}/{len(tasks)}] {basket} {inst}: {len(out)} runs in {secs:.0f}s "
                f"(total {time.time() - t0:.0f}s){' ERRORS ' + str(errs) if errs else ''}",
                flush=True,
            )
    for basket, per_inst in results.items():
        if per_inst:
            with (STORE / f"runs_{basket}.pkl").open("wb") as fh:
                pickle.dump(per_inst, fh)
    print("saved", flush=True)


def report() -> None:
    lines: list[str] = [
        "SUPERTREND MULTI-TIMEFRAME THROUGH THE LAB ENGINE (the product path)",
        "=" * 100,
        "Strategy `supertrend_mtf`, 5-minute decisions, Supertrend(14, 1.7), one position per",
        "symbol, every trade ends at the session close. Five years to 2026-11-07, net of the",
        "modelled costs; index options on modelled premiums (which flatter same-day trades).",
        "mean% = average net result per trade in % of the entry price (premium for an option).",
        "",
    ]
    for basket, variants in VARIANTS.items():
        path = STORE / f"runs_{basket}.pkl"
        if not path.exists():
            continue
        with path.open("rb") as fh:
            per_inst = pickle.load(fh)  # noqa: S301 - our own file
        lines += [basket, "-" * 100]
        for name, params, harness in variants:
            df = d.pool(per_inst, name)
            per = d.by_period(df)
            lines.append(f"{name}")
            shown = {k: v for k, v in params.items() if k not in ("allow_long", "allow_short")}
            lines.append(f"    {shown}")
            if harness:
                lines.append(f"    {harness}")
            lines.append(f"    {'':8}{d.HDR}")
            for p in ("TUNE", "CHOOSE", "TEST", "ALL"):
                lines.append(f"    {p:8}{d.fmt(per[p])}")
            if len(df):
                why = df["exit_reason"].value_counts(normalize=True).mul(100).round(1)
                lines.append("    exits: " + ", ".join(f"{k} {v:.0f}%" for k, v in why.items()))
                by = df.groupby("instrument")["ret_pct"].agg(["size", "mean"])
                lines.append(
                    "    by instrument: "
                    + ", ".join(f"{k} {int(r['size'])} {r['mean']:+.2f}" for k, r in by.iterrows())
                )
                held = df["bars_held"].median() if "bars_held" in df.columns else float("nan")
                lines.append(f"    median bars held (5m): {held:.0f}")
            lines.append("")
    text = "\n".join(lines)
    DEST.write_text(text + "\n")
    print(text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    if args.run:
        run(args.workers, args.only)
    if args.report or not args.run:
        report()


if __name__ == "__main__":
    main()
