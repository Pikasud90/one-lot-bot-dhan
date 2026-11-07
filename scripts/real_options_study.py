"""Re-price the cross-retune's index-option picks with REAL premiums (owner request 2026-11-07).

For every picked setting (data/research/cross_retune_v2_options/final.json), the SAME trades
the Lab simulated are priced three ways:
  model         the retune's Black-76 premiums (India VIX x 1.15), as reported;
  model_noslip  the same model without the underlying slippage the simulator also applied
                (the premium already pays a half-spread and ticks);
  real          the premiums that traded (Dhan expired options, `lab.real_options`), with
                the model's half-spread and ticks and the same charges.
Every comparison is on the trades that could be priced; coverage is reported. Results are
appended per group and index, so an expired token loses nothing already fetched.

Usage:
  .venv/bin/python scripts/real_options_study.py --groups priority [--rate 3]
  .venv/bin/python scripts/real_options_study.py --report
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import cross_retune as cr  # noqa: E402
from expired_options_fetch import INDEX_OPTIONS, STORE_PATH, research_adapter, underlyings  # noqa: E402

from onelot.config import CostConfig  # noqa: E402
from onelot.costs import CostModel, round_to_tick  # noqa: E402
from onelot.data.expired_options import OptionBarStore  # noqa: E402
from onelot.lab.execution import (  # noqa: E402
    OptionPricer,
    execute_trade,
    expiry_datetime,
)
from onelot.lab.real_options import RealPricer, raw_level  # noqa: E402
from onelot.lab.runner import BacktestConfig, DataCache, generate_for_instrument  # noqa: E402
from onelot.lab.simulator import simulate  # noqa: E402
from onelot.settings import current_settings  # noqa: E402

OUT = ROOT / "data" / "research" / "real_options"
PICKS = ROOT / "data" / "research" / "cross_retune_v2_options" / "final.json"
# The three settings that passed tuning and choosing, then the closest per strategy
PRIORITY = [
    ("supertrend_vol", "30m"),
    ("supertrend_vol", "60m"),
    ("ema_cross", "30m"),
    ("nr7_breakout", "15m"),
    ("cpr_breakout", "30m"),
    ("vwap_pullback", "15m"),
    ("turtle_soup", "30m"),
    ("ema_pullback", "15m"),
    ("orb", "60m"),
    ("supertrend_adx", "15m"),
    ("donchian_breakout", "60m"),
    ("mean_reversion", "30m"),
    ("intraday_momentum", "15m"),
]
EDGES = [pd.Timestamp(x, tz="Asia/Kolkata") for x in (cr.T0, cr.TRAIN_END, cr.VALID_END, cr.T_END)]
YEAR_EDGES = [pd.Timestamp(x, tz="Asia/Kolkata") for x in (cr.T0, "2022-11-08", "2023-11-08")]


def period_of(ts: pd.Timestamp) -> str | None:
    for name, lo, hi in zip(("train", "valid", "test"), EDGES[:-1], EDGES[1:], strict=True):
        if lo <= ts < hi:
            return name
    return None


def model_noslip(t, row: dict, spec, bt, costs, pricer: OptionPricer, slip_pct: float) -> float:
    """execute_trade's option branch with the levels the simulator saw BEFORE its underlying
    slippage: same strike, expiry, IV, half-spread, ticks and charges."""
    det = row["detail"]
    opt, k = det["opt_type"], float(det["strike"])
    expiry = expiry_datetime(pd.Timestamp(det["expiry"]).date())
    tick, lot = spec.tick_size, spec.lot_size
    slip = bt.option_slippage_ticks * tick
    half = bt.option_spread_pct
    entry_ts = pd.Timestamp(t.entry_ts)
    raw_entry = t.entry_price / (1 + t.side * slip_pct / 100.0)
    fair0 = pricer.premium(raw_entry, k, entry_ts, expiry, opt)
    entry_fill = round_to_tick(fair0 * (1 + half) + slip, tick, "up")
    gross, cost = 0.0, costs.leg(entry_fill, lot, "buy").total
    for leg in t.legs:
        lvl = raw_level(leg.price, leg.reason, t.side, slip_pct)
        fair = pricer.premium(lvl, k, pd.Timestamp(leg.ts), expiry, opt)
        fill = max(round_to_tick(fair * (1 - half) - slip, tick, "down"), tick)
        q = lot * leg.frac
        gross += (fill - entry_fill) * q
        cost += costs.leg(fill, int(round(q)) or 1, "sell").total * (q / max(int(round(q)), 1))
    return (gross - cost) / (entry_fill * lot) * 100


def run_group(strategy: str, tf: str, params: dict, store, adapter, cache, log) -> list[dict]:
    settings = current_settings()
    bt = settings.backtest
    unds = underlyings()
    p = dict(params)
    direction = p.pop("direction", "both")
    if direction != "both":
        p["allow_long"], p["allow_short"] = direction == "long", direction == "short"
    out: list[dict] = []
    for series_key, inst in INDEX_OPTIONS.items():
        cfg = BacktestConfig.from_settings(strategy, inst, timeframe=tf, params=p)
        spec, strat, f, fine, start_day = generate_for_instrument(cfg, cache)
        if f is None:
            continue
        trades = simulate(f, strat.p, spec.series_key, strategy, cfg.slippage_pct, fine, start_day)
        costs = CostModel(
            CostConfig.load(settings.project_root / "config" / "costs.yaml").product(spec.product)
        )
        pricer = OptionPricer(bt, cache.vix())
        rp = RealPricer(
            unds[series_key], spec, bt, costs, store, cache.bars(series_key, "1m"),
            int(tf.rstrip("m")), source=adapter, slip_pct=cfg.slippage_pct,
        )  # fmt: skip
        t0, n0 = time.time(), 0
        for t in trades:
            row = execute_trade(t, spec, costs, bt, cfg.slippage_pct, pricer)
            ts = pd.Timestamp(t.entry_ts)
            per = period_of(ts)
            if per is None:
                continue
            try:
                rt = rp.price(t, row)
                real = rt.as_dict()
            except Exception as exc:  # noqa: BLE001 - a fetch failure leaves the trade unpriced
                real = {"unpriced": f"error: {type(exc).__name__}: {str(exc)[:160]}"}
                if "TokenExpired" in type(exc).__name__ or "token" in str(exc).lower():
                    log(f"TOKEN problem: {exc}; stopping")
                    raise
            out.append(
                {
                    "strategy": strategy,
                    "tf": tf,
                    "series": series_key,
                    "period": per,
                    "entry_ts": ts.isoformat(),
                    "exit_reason": t.exit_reason,
                    "model_ret": float(row["ret_pct"]),
                    "model_noslip_ret": model_noslip(
                        t, row, spec, bt, costs, pricer, cfg.slippage_pct
                    ),
                    "model_entry_premium": row["detail"]["premium_entry"],
                    "strike": row["detail"]["strike"],
                    "opt": row["detail"]["opt_type"],
                    **{
                        f"real_{k}" if not k.startswith("real_") else k: v
                        for k, v in real.items()
                        if k
                        not in ("entry_ts", "side", "strike", "opt", "model_ret_pct", "model_net")
                    },
                }  # fmt: skip
            )
            n0 += 1
        log(
            f"{strategy} {tf} {series_key}: {n0} trades, {rp.requests} requests, "
            f"{time.time() - t0:.0f}s"
        )
    return out


def stats(x: np.ndarray) -> dict:
    n = int(x.size)
    if n == 0:
        return {"n": 0, "avg": None, "win": None, "t": None}
    sd = float(x.std(ddof=1)) if n > 1 else 0.0
    return {
        "n": n,
        "avg": float(x.mean()),
        "win": float((x > 0).mean() * 100),
        "t": float(x.mean() / sd * math.sqrt(n)) if sd > 0 else None,
    }


def summarize(rows: list[dict]) -> dict:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    priced = df[df["real_ret_pct"].notna()] if "real_ret_pct" in df else df.iloc[0:0]
    out: dict = {"coverage": {}}
    for per in ("train", "valid", "test"):
        d_all = df[df["period"] == per]
        d = priced[priced["period"] == per]
        out["coverage"][per] = f"{len(d)}/{len(d_all)}"
        out[per] = {
            "model_all": stats(d_all["model_ret"].to_numpy(float)),
            "model": stats(d["model_ret"].to_numpy(float)),
            "model_noslip": stats(d["model_noslip_ret"].to_numpy(float)),
            "real": stats(d["real_ret_pct"].to_numpy(float)),
            "breadth_real": _breadth(d, "real_ret_pct"),
            "breadth_model": _breadth(d, "model_ret"),
        }
    out["by_series"] = {
        s: {
            "priced": f"{int(g['real_ret_pct'].notna().sum())}/{len(g)}",
            "real_avg": float(g["real_ret_pct"].mean()) if g["real_ret_pct"].notna().any() else None,
            "model_avg": float(g.loc[g["real_ret_pct"].notna(), "model_ret"].mean())
            if g["real_ret_pct"].notna().any()
            else None,
        }
        for s, g in df.groupby("series")
    }  # fmt: skip
    why = df.loc[df["real_ret_pct"].isna(), "real_unpriced"].fillna("?")
    out["unpriced_reasons"] = (
        why.str.replace(r"\d[\d.]* ", "", regex=True)
        .str.slice(0, 60)
        .value_counts()
        .head(6)
        .to_dict()
    )
    return out


def _breadth(d: pd.DataFrame, col: str) -> str:
    g = d.groupby("series")[col].agg(["count", "mean"])
    g = g[g["count"] >= 3]
    return f"{int((g['mean'] > 0).sum())}/{len(g)}" if len(g) else "-"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", default="priority", help="priority | all | strategy:tf,...")
    ap.add_argument("--rate", type=float, default=3.0)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    picks = {
        tuple(r["group"][:2]): r
        for r in json.loads(PICKS.read_text())
        if r["group"][2] == "OPTIONS"
    }
    if a.groups == "priority":
        groups = [g for g in PRIORITY if g in picks]
    elif a.groups == "all":
        groups = [g for g in PRIORITY if g in picks] + [g for g in picks if g not in PRIORITY]
    else:
        groups = [tuple(x.split(":")) for x in a.groups.split(",")]  # type: ignore[misc]
    if a.report:
        report(groups, picks)
        return
    store = OptionBarStore(STORE_PATH)
    adapter = research_adapter(a.rate)
    cache = DataCache()
    logf = (OUT / "run.log").open("a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    for strategy, tf in groups:
        path = OUT / f"trades_{strategy}_{tf}.jsonl"
        if path.exists():
            log(f"{strategy} {tf}: already done ({path.name})")
            continue
        rows = run_group(strategy, tf, picks[(strategy, tf)]["params"], store, adapter, cache, log)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
        tmp.rename(path)
        s = summarize(rows)
        log(
            f"{strategy} {tf} DONE: coverage {s.get('coverage')} test {s.get('test', {}).get('real')}"
        )
    report(groups, picks)


def fmt(st: dict | None) -> str:
    if not st or not st.get("n"):
        return "–"
    return f"{st['avg']:+.2f}% · {st['win']:.0f}% · {st['n']}"


def report(groups, picks) -> None:
    lines = [
        "| Strategy | TF | Period | Priced | Model (all) | Model (priced) | Model, no double slippage | "
        "REAL premiums | Breadth real |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for strategy, tf in groups:
        path = OUT / f"trades_{strategy}_{tf}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        s = summarize(rows)
        for per in ("train", "valid", "test"):
            q = s[per]
            lines.append(
                f"| {strategy} | {tf} | {per} | {s['coverage'][per]} | {fmt(q['model_all'])} | "
                f"{fmt(q['model'])} | {fmt(q['model_noslip'])} | {fmt(q['real'])} | "
                f"{q['breadth_real']} |"
            )
        (OUT / f"summary_{strategy}_{tf}.json").write_text(json.dumps(s, indent=1, default=str))
    text = "\n".join(lines)
    (OUT / "report.md").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
