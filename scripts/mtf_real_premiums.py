"""Real premiums for the stage-2 finalists on index options, carried trades included.

The stage-2 harness (`mtf_stage2.py`) prices an option trade with Black-76 at India VIX x 1.15
and a last-Tuesday monthly expiry. This script re-prices the SAME trades (same entry minute,
exit minute and strike) with the premiums that actually traded (Dhan expired options, read
through `onelot.lab.real_options`), keeps the model's friction and charges, and reports both
on the trades that could be priced. Read-only market data; nothing is ever sent to a broker
except data requests.

What a carried trade adds to the intraday re-pricing in `RealPricer.price`:

* the contract on the exit day. Dhan serves "near" (1) and "next" (2) monthly series, so a
  contract that was "next" at entry is "near" once the old near contract has expired. Whether
  an expiry fell between entry and exit is read from the market's own days-left estimate
  (`RealPricer._near_days_left`): clearly before or after it from the entry-day estimate,
  and in the wide doubtful zone between from the exit-day estimate, which jumps by about a
  month across an expiry;
* a trade whose NEAR contract expired before the model's exit is not priced (counted);
* a strike more than 10 steps from the money in the exit minute is beyond what Dhan serves:
  not priced, and counted with the model's result for those trades, because they are the
  large moves (a real-premium average without them understates a trend rule).

Usage:
  .venv/bin/python scripts/mtf_real_premiums.py --run [--keys carry30,intra_volc] [--rate 2.5]
  .venv/bin/python scripts/mtf_real_premiums.py --run --offline --limit 20   # cached blocks only
  .venv/bin/python scripts/mtf_real_premiums.py --report
"""

from __future__ import annotations

import argparse
import datetime as dt
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

import mtf_bruteforce as h  # noqa: E402
import mtf_stage2 as s2  # noqa: E402
from expired_options_fetch import (  # noqa: E402
    INDEX_OPTIONS as SERIES_INST,
)
from expired_options_fetch import (  # noqa: E402
    STORE_PATH,
    research_adapter,
    underlyings,
)

from onelot.broker.base import BrokerError, TokenExpiredError  # noqa: E402
from onelot.clock import IST  # noqa: E402
from onelot.config import CostConfig  # noqa: E402
from onelot.costs import CostModel  # noqa: E402
from onelot.data.expired_options import OptionBarStore  # noqa: E402
from onelot.lab.real_options import NeedBlock, RealPricer, RealTrade, interpolate  # noqa: E402

OUT = h.OUT / "real"
DEST = h.OUT / "mtf_real_premiums.txt"

#: the structures re-priced, most important first (names as `mtf_stage2.name2` prints them)
FINALISTS: dict[str, str] = {
    "carry30": "LONG 30m any/daily none gate[all six] trail 30m close <=14:45 carry",
    "intra_volc": "LONG 30m flip<=4 vol_c gate[all six] trail 1d close <=13:00 intra",
    "carry60": "LONG 60m any/once none gate[all six] trail 60m touch <=14:45 carry",
    "slot_vol": "LONG 30m flip<=2 vol_a gate[E only] trail 30m touch <=13:00 intra",
}
SERIES_ORDER = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
PERIOD_OF_YEAR = ("TUNE", "TUNE", "TUNE", "CHOOSE", "TEST")
#: the days-left estimate (a time-value ratio) can be off by about a third either way, so an
#: expiry between entry and exit is taken as certain only well outside that band
SURE_BEFORE = 0.6
SURE_AFTER = 1.8
ROLL_JUMP_DAYS = 10.0  # the estimate jumps by about a month across an expiry
NEXT_EXPIRY_MIN_DAYS = 24.0  # a monthly contract lives at least this long after the one before


def squash(name: str) -> str:
    return " ".join(name.split())


def find_code(name: str) -> np.ndarray:
    want = squash(name)
    for code in s2.structures2():
        if squash(s2.name2(code)) == want:
            return code
    raise SystemExit(f"no stage-2 structure is named {name!r}")


def ist_day(epoch: int) -> dt.date:
    return pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert(IST).date()


class CarryPricer(RealPricer):
    """`RealPricer` for trades given as (entry minute, exit minute, strike), any holding time."""

    def _block(self, key, day):  # type: ignore[no-untyped-def]
        for attempt in range(4):
            try:
                return super()._block(key, day)
            except (NeedBlock, TokenExpiredError):
                raise
            except BrokerError:
                if attempt == 3:
                    raise
                time.sleep(4.0 * (attempt + 1))
        raise AssertionError("unreachable")

    def price_one(
        self, e0: int, x0: int, strike: float, side: int, how: str, level: float, model: dict
    ) -> dict:
        opt = "CE" if side == 1 else "PE"
        day_e, day_x = ist_day(e0), ist_day(x0)
        rt = RealTrade(
            pd.Timestamp(e0, unit="s", tz="UTC").tz_convert(IST),
            side,
            float(strike),
            opt,
            float(model["ret"]),
            float(model["inr"]),
        )
        out: dict = {"code": None, "code_x": None, "left_e": None, "left_x": None}

        def done(why: str | None = None) -> dict:
            rt.unpriced = why
            out.update(
                {
                    "unpriced": why,
                    "real_entry": rt.real_entry,
                    "real_exit": rt.real_exit,
                    "real_ret": rt.real_ret_pct,
                    "real_net": rt.real_net,
                    "entry_wait": rt.entry_wait_min,
                    "exit_wait": rt.exit_wait_min,
                    "rels": rt.rels,
                }
            )
            return out

        raw_left = self._near_days_left(day_e, e0)
        left_e = None if raw_left is None else float(raw_left)
        out["left_e"] = left_e
        code = 1
        if left_e is not None:
            expiry_day = (rt.entry_ts + pd.Timedelta(seconds=left_e * 86400.0)).date()
            if (expiry_day - day_e).days < float(self.bt.option_min_days_to_expiry):
                code = 2  # the model's roll: never the contract in its last days
        out["code"] = code
        hit = self._find(code, opt, strike, e0, day_e, self.max_wait)
        if isinstance(hit, str):
            return done(self._why(hit, opt, strike, "of entry"))
        rt.entry_wait_min, rt.rels = hit[0], [hit[2]]
        p_entry = float(hit[1][0])

        code_x = code
        if day_x != day_e:
            if left_e is None:
                return done("days to expiry unknown at entry (carried trade)")
            elapsed = float((day_x - day_e).days)
            if elapsed > left_e + NEXT_EXPIRY_MIN_DAYS:
                return done("held beyond the following expiry as well")
            if elapsed <= SURE_BEFORE * left_e - 1.0:
                crossed = False
            elif elapsed >= SURE_AFTER * left_e + 3.0:
                crossed = True
            else:
                raw_x = self._near_days_left(day_x, x0)
                left_x = None if raw_x is None else float(raw_x)
                out["left_x"] = left_x
                if left_x is None:
                    return done("cannot tell whether the near contract expired before the exit")
                crossed = left_x > max(left_e - elapsed, 0.0) + ROLL_JUMP_DAYS
            if crossed:
                if code == 1:
                    return done("the near contract expired before the model's exit")
                code_x = 1
        out["code_x"] = code_x
        found = self._find(code_x, opt, strike, x0, day_x, self.max_wait)
        if isinstance(found, str):
            return done(self._why(found, opt, strike, "of the exit"))
        wait, row, rel = found
        rt.exit_wait_min = wait
        rt.rels.append(rel)
        if how == "touch" and wait == 0:
            upos = self._u_pos[x0]
            px = interpolate(
                (row[0], row[1], row[2], row[3]),
                tuple(self._u[upos]),  # type: ignore[arg-type]
                level,
                opt,
            )
        elif how == "close" and wait == 0:
            px = float(row[3])
        else:
            px = float(row[0])
        self._book(rt, p_entry, [(px, 1.0)])
        return done(None)


_PREP: dict[str, h.Prep] = {}


def trades_of(p: h.Prep, code: np.ndarray) -> dict:
    es = s2.entry_state(p, s2.E_TFS[int(code[1])])
    return s2.trades_for(p, es, code)


def exit_how(p: h.Prep, tr: dict, i: int, xm: int) -> str:
    """How the harness filled exit `i`: at a bar `open`, inside the bar (`touch`), or `close`."""
    x = int(tr["X"][i])
    if not bool(tr["hit"][i]):  # the forced exit: session end or the model's expiry session
        s_x = int(p.sess[x])
        return "close" if bool(p.xclose[s_x]) and x == int(p.xi[s_x]) else "open"
    if xm != h.TOUCH:
        return "open"
    return "open" if abs(float(tr["PX"][i]) - float(p.o[x])) < 1e-9 else "touch"


def run_key(key: str, series: str, store, adapter, limit: int | None, log) -> None:  # noqa: ANN001
    path = OUT / f"{key}_{series}.jsonl"
    if path.exists() and limit is None:
        log(f"{key} {series}: already done ({path.name})")
        return
    code = find_code(FINALISTS[key])
    inst = SERIES_INST[series]
    if inst not in _PREP:  # one index in memory at a time
        _PREP.clear()
        _PREP[inst] = h.prepare(inst)
    p = _PREP[inst]
    tr = trades_of(p, code)
    n = len(tr["E"])
    side, xm = int(code[0]), int(code[6])
    spec = h._cache.spec(inst)
    bt = h._settings.backtest
    costs = CostModel(
        CostConfig.load(h._settings.project_root / "config" / "costs.yaml").product(spec.product)
    )
    und_1m = pd.DataFrame({"open": p.o, "high": p.h, "low": p.l, "close": p.c}, index=p.index)
    rp = CarryPricer(
        underlyings()[series], spec, bt, costs, store, und_1m, 1, source=adapter, slip_pct=0.0
    )
    strike = p.opt(side).strike
    fair = p.opt(side).fair
    rows: list[dict] = []
    t0 = time.time()
    todo = range(n) if limit is None else range(0, n, max(n // limit, 1))
    for i in todo:
        e, x = int(tr["E"][i]), int(tr["X"][i])
        e0, x0 = int(p.ns[e] // 1_000_000_000), int(p.ns[x] // 1_000_000_000)
        how = exit_how(p, tr, i, xm)
        model = {"ret": float(tr["ret"][i]), "inr": float(tr["inr"][i])}
        base = {
            "key": key,
            "series": series,
            "period": PERIOD_OF_YEAR[int(p.year[e])],
            "year": int(p.year[e]),
            "entry_ts": str(p.index[e]),
            "exit_ts": str(p.index[x]),
            "sessions": int(p.sess[x] - p.sess[e]),
            "by_line": bool(tr["hit"][i]),
            "how": how,
            "strike": float(strike[e]),
            "model_ret": model["ret"],
            "model_inr": model["inr"],
            "model_fair_entry": float(fair[e]),
            "index_move_pct": float((tr["PX"][i] / p.o[e] - 1) * 100),
        }
        try:
            real = rp.price_one(e0, x0, float(strike[e]), side, how, float(tr["PX"][i]), model)
        except NeedBlock:
            real = {"unpriced": "block not in the store (offline run)"}
        except TokenExpiredError as exc:
            log(f"TOKEN problem: {type(exc).__name__}; stopping (renew the token and rerun)")
            raise SystemExit(2) from None
        except Exception as exc:  # noqa: BLE001 - one bad trade never stops the study
            real = {"unpriced": f"error: {type(exc).__name__}: {str(exc)[:120]}"}
        rows.append({**base, **real})
        if len(rows) % 100 == 0:
            priced = sum(1 for r in rows if r.get("real_ret") is not None)
            log(
                f"{key} {series}: {len(rows)}/{n} trades, {priced} priced, "
                f"{rp.requests} requests, {time.time() - t0:.0f}s"
            )
    priced = sum(1 for r in rows if r.get("real_ret") is not None)
    log(
        f"{key} {series} DONE: {len(rows)} trades, {priced} priced, {rp.requests} requests, "
        f"{time.time() - t0:.0f}s"
    )
    if limit is None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
        tmp.rename(path)
    else:
        for r in rows[:8]:
            log(json.dumps({k: r.get(k) for k in (
                "entry_ts", "exit_ts", "how", "strike", "code", "code_x", "left_e", "model_ret",
                "real_ret", "model_fair_entry", "real_entry", "unpriced")}, default=str))  # fmt: skip


# ───────────────────────────── report ─────────────────────────────────────


def day_t(df: pd.DataFrame, col: str) -> float:
    """t of the mean of `col`, trades of one entry day treated as one observation."""
    x = df[col].to_numpy(float)
    n = len(x)
    if n < 3:
        return float("nan")
    m = x.mean()
    days = df["entry_ts"].str.slice(0, 10)
    g = pd.DataFrame({"d": days.to_numpy(), "x": x}).groupby("d")["x"].agg(["sum", "count"])
    dev = g["sum"].to_numpy() - m * g["count"].to_numpy()
    se = math.sqrt(float((dev * dev).sum())) / n
    return m / se if se > 0 else float("nan")


def cell(df: pd.DataFrame, col: str) -> str:
    if df.empty:
        return f"{'-':>18}"
    return f"{df[col].mean():+8.3f} (t{day_t(df, col):+5.1f})"


def report() -> None:
    lines: list[str] = []
    w = lines.append
    w("REAL PREMIUMS FOR THE STAGE-2 FINALISTS (index options)")
    w("=" * 100)
    w("The same trades priced twice: MODEL = Black-76 at India VIX x 1.15 (stage 2), REAL = the")
    w("premiums that traded (Dhan expired options), same friction and charges. % of premium, net,")
    w("per trade; t treats the trades of one entry day as one observation. A trade is UNPRICED")
    w("when its strike did not trade near a needed minute or lay beyond what Dhan serves.")
    w("")
    for key, name in FINALISTS.items():
        files = [OUT / f"{key}_{s}.jsonl" for s in SERIES_ORDER]
        rows = [json.loads(x) for f in files if f.exists() for x in f.read_text().splitlines() if x]
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if "real_ret" not in df:
            df["real_ret"] = np.nan
        df["real_ret"] = pd.to_numeric(df["real_ret"], errors="coerce")
        df["priced"] = df["real_ret"].notna()
        have = [s for s in SERIES_ORDER if (df["series"] == s).any()]
        w("-" * 100)
        w(f"[{key}] {squash(name)}")
        w(f"series re-priced: {', '.join(have)}")
        w("")
        w(f"{'':<22}{'trades':>7}{'priced':>8}  {'MODEL all trades':>18}  {'MODEL priced':>18}  "
          f"{'REAL priced':>18}  {'REAL-MODEL':>10}")  # fmt: skip

        def line(label: str, d: pd.DataFrame) -> None:
            pr = d[d["priced"]]
            diff = (pr["real_ret"] - pr["model_ret"]).mean() if len(pr) else float("nan")
            w(f"{label:<22}{len(d):>7}{len(pr):>8}  {cell(d, 'model_ret')}  "  # noqa: B023
              f"{cell(pr, 'model_ret')}  {cell(pr, 'real_ret')}  {diff:>+10.3f}")  # fmt: skip

        for per in ("TUNE", "CHOOSE", "TEST"):
            line(per, df[df["period"] == per])
        line("ALL", df)
        w("")
        for s in have:
            line(s, df[df["series"] == s])
        w("")
        liquid = df[df["series"].isin(["NIFTY", "BANKNIFTY"])]
        for per in ("TUNE", "CHOOSE", "TEST"):
            line(f"NIFTY+BANKNIFTY {per}", liquid[liquid["period"] == per])
        line("NIFTY+BANKNIFTY ALL", liquid)
        w("")
        line("exit same session", df[df["sessions"] == 0])
        line("held 1 session", df[df["sessions"] == 1])
        line("held 2+ sessions", df[df["sessions"] >= 2])
        w("")
        pr = df[df["priced"]]
        if len(pr):
            ratio = (pr["real_entry"].astype(float) / pr["model_fair_entry"]).median()
            d = pr["real_ret"] - pr["model_ret"]
            w(f"entry premium, real / model (median): {ratio:.3f}   "
              f"REAL-MODEL per trade: median {d.median():+.2f}, "
              f"quartiles {d.quantile(0.25):+.2f} / {d.quantile(0.75):+.2f}")  # fmt: skip
            w(f"real: win {100 * (pr['real_ret'] > 0).mean():.1f}%  "
              f"model on the same trades: win {100 * (pr['model_ret'] > 0).mean():.1f}%")  # fmt: skip
        un = df[~df["priced"]].copy()
        if len(un):
            un["why"] = (
                un["unpriced"]
                .fillna("?")
                .str.replace(r"(CE|PE) [\d.]+", r"\1", regex=True)
                .str.replace(r"\d+ min", "3 min", regex=True)
                .str.slice(0, 70)
            )
            w("")
            w("unpriced trades and what the MODEL says about them:")
            g = un.groupby("why")["model_ret"].agg(["count", "mean"]).sort_values("count")[::-1]
            for why, r in g.iterrows():
                w(f"  {int(r['count']):>5}  model {r['mean']:+8.3f}%  {why}")
        w("")
    text = "\n".join(lines) + "\n"
    DEST.write_text(text, encoding="utf-8")
    print(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--keys", default=",".join(FINALISTS))
    ap.add_argument("--series", default=",".join(SERIES_ORDER))
    ap.add_argument("--rate", type=float, default=2.5)
    ap.add_argument("--offline", action="store_true", help="cached blocks only, no requests")
    ap.add_argument("--limit", type=int, default=None, help="a sample of trades; nothing is saved")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.run:
        store = OptionBarStore(STORE_PATH)
        adapter = None if a.offline else research_adapter(a.rate)
        logf = (OUT / "run.log").open("a", encoding="utf-8")

        def log(msg: str) -> None:
            line = f"[{time.strftime('%H:%M:%S')}] {msg}"
            print(line, flush=True)
            logf.write(line + "\n")
            logf.flush()

        for series in a.series.split(","):
            for key in a.keys.split(","):
                run_key(key.strip(), series.strip(), store, adapter, a.limit, log)
    if a.report:
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
