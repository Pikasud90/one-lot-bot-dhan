"""Drive a RUNNING Simulator (a private instance with real bars seeded, see
scripts/dev_seed_bars.py) through its HTTP API and check every answer. Not collected by pytest.

    PYTHONPATH=src .venv/bin/python tests/sim/tools/drive_sim.py run   [--only NAME] [--snapshot F]
    PYTHONPATH=src .venv/bin/python tests/sim/tools/drive_sim.py compare F    # after a restart

It needs the instance's database in `data/` (read for the seeded bars, the sessions' detail
and the option store). Never point it at the owner's desk: the base URL defaults to port 8798.

What `run` checks, per session (create a past session, advance through whole days in mixed
step sizes, take EVERY alert):
  contract keys and types, strict JSON, `events` seq strictly increasing, the cursor never
  goes back, `minutes` are exactly the stored bars in order; the fill is at the open of the
  first minute after the take with the documented slippage (stock) or the stored/modelled
  premium with the Lab's spread and ticks (option); the ratchet replayed with
  `manage/ratchet.py` gives every STAGE / STOP_MOVED / TARGET_MOVED / CLOSED event, level and
  reason; gross, charges, net, r_multiple add up; the summary is the sum of the trades;
  /api/sim/trades and /trades.csv list the same closed trades; real against modelled premiums
  are counted; timings.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from onelot.clock import IST
from onelot.data.expired_options import Underlying
from onelot.data.resample import resample_bars
from onelot.data.universe import load_specs
from onelot.indicators import core as ind
from onelot.lab.real_options import interpolate
from onelot.manage import ratchet
from onelot.sim.bars import StoreBars
from onelot.sim.engine import ATR_PERIOD
from onelot.sim.manager import default_deps
from onelot.sim.pricing import Contract, Pricer, StoredPremiums
from onelot.store import init_database

ROOT = Path(__file__).resolve().parents[3]
BASE = "http://127.0.0.1:8798"
MIN = pd.Timedelta(minutes=1)
INDEX_KEYS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY")
SLIP = 0.0003  # slippage_pct 0.03 %

NUM = (int, float)
SESSION_KEYS = {
    "id": str, "instrument": dict, "strategy": str, "timeframe": str, "tf_minutes": int,
    "mode": str, "start": str, "cursor": str, "status": str, "capital": NUM, "params": dict,
    "premium_note": str,
}  # fmt: skip
ADVANCE_KEYS = {
    "cursor": str, "minutes": list, "events": list, "alert": (dict, type(None)),
    "trade": (dict, type(None)), "summary": dict, "last_price": (NUM, type(None)),
    "over": bool, "day_end": bool,
}  # fmt: skip
STATE_KEYS = {
    "session": dict, "alert": (dict, type(None)), "trade": (dict, type(None)),
    "summary": dict, "trades": list,
}  # fmt: skip
ALERT_KEYS = {
    "id": str, "ts": str, "expires": str, "side": str, "action": str, "entry": NUM,
    "stop": NUM, "target": NUM, "risk": NUM, "reward": NUM, "qty": int, "traded": dict,
    "risk_inr": (NUM, type(None)), "reward_inr": (NUM, type(None)), "costs_inr": NUM,
    "reasons": list,
}  # fmt: skip
TRADE_KEYS = {
    "id": int, "status": str, "side": str, "action": str, "qty": int, "entry_ts": (str, type(None)),
    "entry": (NUM, type(None)), "entry_price": (NUM, type(None)), "source": (str, type(None)),
    "stop": (NUM, type(None)), "target": (NUM, type(None)), "stop0": (NUM, type(None)),
    "target0": (NUM, type(None)), "stage": str, "best": (NUM, type(None)),
    "progress": (NUM, type(None)), "mark": (NUM, type(None)), "mark_price": (NUM, type(None)),
    "open_pnl": (NUM, type(None)), "exit_ts": (str, type(None)), "exit": (NUM, type(None)),
    "exit_price": (NUM, type(None)), "exit_reason": (str, type(None)),
    "gross": (NUM, type(None)), "charges": (NUM, type(None)), "net": (NUM, type(None)),
    "r_multiple": (NUM, type(None)), "traded": dict,
}  # fmt: skip
SUMMARY_KEYS = {
    "trades": int, "winners": int, "net": NUM, "gross": NUM, "charges": NUM,
    "avg_win": (NUM, type(None)), "avg_loss": (NUM, type(None)), "best": (NUM, type(None)),
    "worst": (NUM, type(None)), "equity": NUM,
}  # fmt: skip
EVENT_KINDS = {
    "ALERT", "ALERT_EXPIRED", "FILLED", "STAGE", "STOP_MOVED", "TARGET_MOVED", "EXIT_SIGNAL",
    "CLOSED", "DAY_END", "DATA_END",
}  # fmt: skip
REASONS = {
    "STOP", "BREAKEVEN_STOP", "LOCK_STOP", "TRAIL_STOP", "GAP_STOP", "GAP_BREAKEVEN_STOP",
    "GAP_LOCK_STOP", "GAP_TRAIL_STOP", "TARGET", "GAP_TARGET", "SIGNAL_EXIT", "SESSION_END",
    "MANUAL", "DATA_END",
}  # fmt: skip


class Problems:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.checks = 0

    def check(self, ok: bool, what: str) -> bool:
        self.checks += 1
        if not ok:
            self.items.append(what)
            if len(self.items) <= 60:
                print("  PROBLEM:", what)
        return ok


P = Problems()


def _strict(name: str) -> Any:
    raise ValueError(f"non-strict JSON constant {name}")


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base, token
        self.calls = 0

    def req(self, method: str, path: str, body: Any = None, auth: bool = True) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("content-type", "application/json")
        if auth and method == "POST":
            r.add_header("x-onelot-token", self.token)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                status, raw = resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read().decode()
        self.calls += 1
        if path.split("?")[0].endswith(".csv"):
            return status, raw
        try:
            return status, json.loads(raw, parse_constant=_strict)
        except ValueError:
            P.check(False, f"{method} {path}: not strict JSON: {raw[:120]!r}")
            return status, None

    def get(self, path: str) -> tuple[int, Any]:
        return self.req("GET", path)

    def post(self, path: str, body: Any = None, auth: bool = True) -> tuple[int, Any]:
        return self.req("POST", path, {} if body is None else body, auth)


def shape(obj: Any, spec: dict[str, Any], where: str) -> None:
    if not P.check(isinstance(obj, dict), f"{where}: not an object"):
        return
    for k, t in spec.items():
        if not P.check(k in obj, f"{where}: key {k!r} missing"):
            continue
        v = obj[k]
        ok = isinstance(v, t) and not (
            isinstance(v, bool)
            and t is not bool
            and bool not in (t if isinstance(t, tuple) else (t,))
        )
        P.check(ok, f"{where}: {k} is {type(v).__name__}, want {t}")


def iso_ns(s: str) -> int:
    return int(pd.Timestamp(s).as_unit("ns").value)


def ts_iso(sec: int) -> str:
    return pd.Timestamp(sec, unit="s", tz="UTC").tz_convert(IST).isoformat()


# ── the world: the stored bars the server read, the option store, the cost tables ─────────
class World:
    def __init__(self) -> None:
        self.db_path = ROOT / "data" / "onelot.db"
        self.opt_path = ROOT / "data" / "expired_options.db"
        self.engine = init_database(f"sqlite:///{self.db_path}").engine
        self.bars = StoreBars(self.engine)
        self.series, self.specs = load_specs()
        self.deps = default_deps(None, option_store_path=self.opt_path)
        self.frames: dict[str, pd.DataFrame] = {}
        self._stored: dict[str, StoredPremiums] = {}
        self.stock_costs = self.deps.stock_costs
        self.option_costs = self.deps.option_costs

    def frame(self, key: str) -> pd.DataFrame:
        if key not in self.frames:
            self.frames[key] = self.bars.minute_bars(key, None, None)
        return self.frames[key]

    def stored(self, key: str) -> StoredPremiums | None:
        if key not in self._stored:
            spec = self.specs[f"{key}_OPT"]
            und = Underlying.from_specs(self.series[key], spec)
            if self.deps.option_store is None:
                return None
            self._stored[key] = StoredPremiums(
                self.deps.option_store, und, spec, self.deps.bt, self.deps.option_costs,
                self.frame(key),
            )  # fmt: skip
        return self._stored[key]

    def pricer(self, key: str) -> Pricer:
        return Pricer(self.specs[f"{key}_OPT"], "INDEX", self.deps.bt, self.deps.option_costs)

    def detail_rows(self, sid: str) -> dict[int, dict[str, Any]]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT id, detail FROM sim_trades WHERE session_id=?", (sid,))
            return {int(i): json.loads(d) if d else {} for i, d in rows}
        finally:
            con.close()

    def atr_at(self, key: str, t0: pd.Timestamp, tf: str) -> float:
        """ATR(14, Wilder) of `tf` bars, read on the last bar closed at or before `t0`."""
        f = self.frame(key)
        f = f[f.index < t0]  # nothing from the future
        ab = resample_bars(f, tf)
        atr = ind.atr(ab["high"], ab["low"], ab["close"], ATR_PERIOD)
        ends = []
        for t in ab.index:
            day_close = t.normalize() + pd.Timedelta(hours=15, minutes=30)
            ends.append(min(t + pd.Timedelta(minutes=int(tf[:-1])), day_close))
        ok = [i for i, e in enumerate(ends) if e <= t0]
        return float(atr.iloc[ok[-1]]) if ok else float("nan")


# ── a session being driven ────────────────────────────────────────────────────────────────
class Run:
    def __init__(
        self, api: Api, world: World, name: str, body: dict[str, Any], days: int, seed: int
    ):
        self.api, self.w, self.name, self.body, self.days = api, world, name, body, days
        self.rng = random.Random(seed)
        self.key = body["instrument"]
        self.latest: dict[str, Any] = {"alert": None, "trade": None}
        self.minutes: list[tuple[int, float, float, float, float]] = []  # revealed so far
        self.events: list[dict[str, Any]] = []
        self.last_seq = 0
        self.cursor_ns = 0
        self.sid = ""
        self.alerts_seen = 0
        self.taken = 0
        self.sizes = Counter()
        self.advance_ms: list[float] = []
        self.created_ms = 0.0
        self.day_ends = 0
        self.iv_sources: Counter = Counter()
        self.over = False
        self.state: dict[str, Any] = {}
        self.nxt = 0  # index in the stored frame of the next bar to be revealed

    # ── calls ───────────────────────────────────────────────────────────────
    def create(self) -> None:
        t = time.perf_counter()
        st, out = self.api.post("/api/sim/sessions", self.body)
        self.created_ms = (time.perf_counter() - t) * 1000
        if not P.check(st == 200, f"{self.name}: create -> {st} {out}"):
            raise RuntimeError("create failed")
        shape(out, {"session": dict, "candles": list, "state": dict}, f"{self.name} create")
        shape(out["session"], SESSION_KEYS, f"{self.name} create.session")
        shape(out["state"], STATE_KEYS, f"{self.name} create.state")
        self.sid = out["session"]["id"]
        self.latest = {"alert": out["state"]["alert"], "trade": out["state"]["trade"]}
        self.cursor_ns = iso_ns(out["session"]["cursor"])
        fr = self.w.frame(self.key)
        idx = fr.index.as_unit("ns").asi8
        self.idx = idx
        self.nxt = int(idx.searchsorted(self.cursor_ns, side="right"))
        self.premium_note = out["session"]["premium_note"]
        for c in out["candles"]:
            P.check(
                len(c) == 6 and all(isinstance(x, NUM) for x in c), f"{self.name}: candle shape {c}"
            )
        P.check(
            all(
                c[0] + 60 * out["session"]["tf_minutes"] <= self.cursor_ns // 10**9 + 60
                for c in out["candles"]
            ),
            f"{self.name}: a candle in `candles` is not closed at the cursor",
        )
        P.check(len(out["candles"]) <= 300, f"{self.name}: more than 300 candles")

    def advance(self, n: int) -> dict[str, Any]:
        t = time.perf_counter()
        st, r = self.api.post(f"/api/sim/sessions/{self.sid}/advance", {"minutes": n})
        ms = (time.perf_counter() - t) * 1000
        if n == 1:
            self.advance_ms.append(ms)
        self.sizes[n] += 1
        assert st == 200, (st, r)
        self.check_advance(r, n)
        self.latest = {"alert": r["alert"], "trade": r["trade"]}
        return r

    def check_advance(self, r: dict[str, Any], n: int) -> None:
        w = self.name
        shape(r, ADVANCE_KEYS, f"{w} advance")
        fr = self.w.frame(self.key)
        mins = r["minutes"]
        # minutes are exactly the stored bars, in order
        exp = fr.iloc[self.nxt : self.nxt + len(mins)]
        ok = len(exp) == len(mins)
        for m, (ts, row) in zip(mins, exp.iterrows(), strict=False):
            ok = ok and m[0] == int(ts.value // 10**9)
            ok = ok and all(
                abs(float(m[1 + j]) - float(row[c])) < 0.006
                for j, c in enumerate(["open", "high", "low", "close"])
            )
        P.check(ok, f"{w}: minutes differ from the stored bars at {r['cursor']}")
        if mins:
            P.check(len(mins) <= n, f"{w}: {len(mins)} minutes for advance({n})")
            if len(mins) < n and not (r["day_end"] or r["over"]):
                P.check(False, f"{w}: advance({n}) gave {len(mins)} with no day_end/over")
        for m in mins:
            self.minutes.append((m[0], m[1], m[2], m[3], m[4]))
        self.nxt += len(mins)
        if mins:
            P.check(
                iso_ns(r["cursor"]) == mins[-1][0] * 10**9,
                f"{w}: cursor is not the last minute's start",
            )
        P.check(iso_ns(r["cursor"]) >= self.cursor_ns, f"{w}: the cursor went back")
        self.cursor_ns = iso_ns(r["cursor"])
        for e in r["events"]:
            shape(e, {"seq": int, "ts": str, "kind": str, "text": str, "data": dict}, f"{w} event")
            P.check(e["seq"] > self.last_seq, f"{w}: seq {e['seq']} not above {self.last_seq}")
            P.check(e["kind"] in EVENT_KINDS, f"{w}: unknown event kind {e['kind']}")
            P.check(e["ts"].endswith("+05:30"), f"{w}: event ts without +05:30: {e['ts']}")
            self.last_seq = max(self.last_seq, e["seq"])
            self.events.append(e)
        if r["alert"]:
            shape(r["alert"], ALERT_KEYS, f"{w} alert")
        if r["trade"]:
            shape(r["trade"], TRADE_KEYS, f"{w} trade")
        shape(r["summary"], SUMMARY_KEYS, f"{w} summary")
        # every ALERT is a candle close; kinds carry ids in data
        for e in r["events"]:
            if e["kind"] == "ALERT":
                P.check("alert_id" in e["data"], f"{w}: ALERT without data.alert_id")
            if e["kind"] in ("FILLED", "CLOSED"):
                P.check(
                    "trade_id" in e["data"] and "net" in e["data"] or e["kind"] == "FILLED",
                    f"{w}: {e['kind']} without data",
                )
        if r["over"]:
            self.over = True
        if r["day_end"]:
            self.day_ends += 1

    # ── the loop ────────────────────────────────────────────────────────────
    def drive(self) -> None:
        self.create()
        guard = 0
        while self.day_ends < self.days and not self.over and guard < 20000:
            guard += 1
            s = self.latest
            if s["alert"] is not None:
                a = s["alert"]
                self.alerts_seen += 1
                st, ans = self.api.post(f"/api/sim/sessions/{self.sid}/take", {"alert_id": a["id"]})
                if P.check(st == 200, f"{self.name}: take -> {st} {ans}"):
                    self.check_take(ans, a)
                    self.taken += 1
                    self.latest = {"alert": None, "trade": ans["trade"]}
                    self.advance(1)  # the fill minute
                    continue
            busy = s["trade"] is not None
            pool = [1, 1, 1, 2, 5, 15, 60] if busy else [1, 1, 2, 3, 5, 7, 10]
            if not busy and self.rng.random() < 0.04:
                pool = [390]
            self.advance(self.rng.choice(pool))
        self.final = self.get()

    def check_take(self, ans: dict[str, Any], alert: dict[str, Any]) -> None:
        shape(ans, STATE_KEYS, f"{self.name} take")
        P.check("candles" not in ans, f"{self.name}: take carries candles")
        t = ans.get("trade") or {}
        P.check(t.get("status") == "PENDING_FILL", f"{self.name}: take -> status {t.get('status')}")
        P.check(ans.get("alert") is None, f"{self.name}: alert still there after take")
        P.check(
            t.get("action") == alert["action"] and t.get("qty") == alert["qty"],
            f"{self.name}: take changed the action/qty",
        )

    def get(self) -> dict[str, Any]:
        st, g = self.api.get(f"/api/sim/sessions/{self.sid}")
        P.check(st == 200, f"{self.name}: GET -> {st}")
        shape(g, {**STATE_KEYS, "candles": list}, f"{self.name} GET")
        shape(g["session"], SESSION_KEYS, f"{self.name} GET.session")
        return g

    # ── after the run: replay every trade ───────────────────────────────────
    def audit(self) -> list[dict[str, Any]]:
        w, g = self.name, self.final
        detail = self.w.detail_rows(self.sid)
        closed = g["trades"]
        sm = g["summary"]
        nets = [t["net"] for t in closed]
        P.check(sm["trades"] == len(closed), f"{w}: summary.trades {sm['trades']} != {len(closed)}")
        P.check(
            abs(sm["net"] - sum(nets)) < 0.011 * max(1, len(nets)),
            f"{w}: summary.net != sum of nets",
        )
        P.check(
            abs(sm["gross"] - sum(t["gross"] for t in closed)) < 0.011 * max(1, len(nets)),
            f"{w}: summary.gross",
        )
        P.check(
            abs(sm["charges"] - sum(t["charges"] for t in closed)) < 0.011 * max(1, len(nets)),
            f"{w}: summary.charges",
        )
        P.check(
            abs(sm["equity"] - (g["session"]["capital"] + sm["net"])) < 0.02, f"{w}: summary.equity"
        )
        P.check(sm["winners"] == sum(1 for x in nets if x > 0), f"{w}: summary.winners")
        # no trade left dangling in a finished day / ended session
        if self.over or g["session"]["status"] in ("DAY_END", "ENDED"):
            P.check(g["trade"] is None, f"{w}: a trade is still open at the end of the data/day")
        rows: list[dict[str, Any]] = []
        for t in sorted(closed, key=lambda r: r["id"]):
            rows.append(self.audit_trade(t, detail.get(t["id"], {})))
        return rows

    def bar_at(self, ts_sec: int) -> int | None:
        for i in range(len(self.minutes) - 1, -1, -1):
            if self.minutes[i][0] == ts_sec:
                return i
            if self.minutes[i][0] < ts_sec:
                break
        return None

    def audit_trade(self, t: dict[str, Any], det: dict[str, Any]) -> dict[str, Any]:
        w = f"{self.name} trade {t['id']}"
        shape(t, TRADE_KEYS, w)
        P.check(t["status"] == "CLOSED", f"{w}: status {t['status']}")
        P.check(t["exit_reason"] in REASONS, f"{w}: reason {t['exit_reason']}")
        is_opt = self.key in INDEX_KEYS
        side = 1 if t["side"] == "LONG" else -1
        evs = [e for e in self.events if e["data"].get("trade_id") == t["id"] or False]
        fill = next((e for e in evs if e["kind"] == "FILLED"), None)
        close = next((e for e in evs if e["kind"] == "CLOSED"), None)
        out = {
            "name": self.name, "id": t["id"], "opt": is_opt, "entry_source": det.get("entry_source"),
            "exit_source": det.get("exit_source"),
            "reason": t["exit_reason"], "net": t["net"], "r": t["r_multiple"],
        }  # fmt: skip
        if not (
            P.check(fill is not None, f"{w}: no FILLED event")
            and P.check(close is not None, f"{w}: no CLOSED event")
        ):
            return out
        f0 = int(pd.Timestamp(fill["ts"]).value // 10**9)
        c_ts = int(pd.Timestamp(close["ts"]).value // 10**9)
        i0 = self.bar_at(f0)
        if not P.check(
            i0 is not None, f"{w}: fill minute {fill['ts']} not among the revealed bars"
        ):
            return out
        # a fill is at the first minute after the take: the minute before it was the cursor of the take
        o = self.minutes[i0][1]
        if is_opt:
            P.check(abs(t["entry"] - o) < 0.006, f"{w}: managed entry {t['entry']} != bar open {o}")
        else:
            P.check(
                abs(t["entry_price"] - o * (1 + side * SLIP)) < 0.006,
                f"{w}: stock entry {t['entry_price']} != open {o} with 0.03% slippage",
            )
            P.check(abs(t["entry"] - t["entry_price"]) < 0.006, f"{w}: stock entry != entry_price")
        # first stop = k x ATR on the strategy's 60m bars read before the fill
        k = self.final["session"]["params"]["stop_atr_k"]
        tr = self.final["session"]["params"]["target_r"]
        tf = (
            "60m"
            if (
                self.final["session"]["params"]["stop_atr_tf"] == "60m"
                and self.final["session"]["tf_minutes"] < 60
            )
            else self.final["session"]["timeframe"]
        )
        atr = self.w.atr_at(self.key, pd.Timestamp(f0, unit="s", tz="UTC").tz_convert(IST), tf)
        entry_x = o if is_opt else o * (1 + side * SLIP)  # the managed entry, unrounded
        risk = abs(t["entry"] - t["stop0"])
        if atr == atr:
            P.check(
                abs(risk - k * atr) < 0.02, f"{w}: first risk {risk:.3f} != {k} x ATR {atr:.3f}"
            )
            risk = k * atr
        # replay the ratchet bar by bar
        cost = float((det.get("plan") or {}).get("cost", 0.0))
        plan = ratchet.plan_from_risk(side, entry_x, risk, tr, cost)
        P.check(
            abs(plan.stop - t["stop0"]) < 0.011 and abs(plan.target - t["target0"]) < 0.011,
            f"{w}: first stop/target {t['stop0']}/{t['target0']} != replay {plan.stop:.2f}/{plan.target:.2f}",
        )
        rp = ratchet.RatchetParams(extend=bool(self.final["session"]["params"]["extend"]))
        state = ratchet.start(plan)
        mine: list[tuple[int, str, Any]] = []
        theirs: list[tuple[int, str, Any]] = []
        lo_seq, hi_seq = fill["seq"], close["seq"]
        for e in self.events:
            if lo_seq < e["seq"] < hi_seq and e["kind"] in ("STAGE", "STOP_MOVED", "TARGET_MOVED"):
                v = e["data"].get("stage") or e["data"].get("stop") or e["data"].get("target")
                theirs.append((int(pd.Timestamp(e["ts"]).value // 10**9), e["kind"], v))
        exit_level = None
        reason_expected = None
        k_bar = None
        for j in range(i0, len(self.minutes)):
            ts, bo, bh, bl, bc = self.minutes[j]
            if (
                ts * 1 == c_ts - 0 and True
            ):  # closed at this bar's open (gap, signal, manual, session)
                reason_expected = t["exit_reason"]
                k_bar = j
                if reason_expected.startswith("GAP_"):
                    hit = ratchet.exit_on_bar(plan, state, bo, bh, bl)
                    P.check(
                        hit is not None and hit[0] == reason_expected and abs(hit[1] - bo) < 0.006,
                        f"{w}: replay says {hit}, engine closed {reason_expected} at the open",
                    )
                exit_level = bo
                break
            hit = ratchet.exit_on_bar(plan, state, bo, bh, bl)
            if hit is not None:
                if ts + 60 == c_ts or ts == c_ts:
                    P.check(
                        hit[0] == t["exit_reason"],
                        f"{w}: replay exit {hit[0]} != engine {t['exit_reason']}",
                    )
                    exit_level = hit[1]
                else:
                    P.check(
                        False,
                        f"{w}: replay exits ({hit}) at {ts_iso(ts)} but the engine closed at {close['ts']} ({t['exit_reason']})",
                    )
                    exit_level = hit[1]
                k_bar = j
                break
            nxt_open = j + 1 < len(self.minutes) and self.minutes[j + 1][0] == c_ts
            if ts + 60 == c_ts and t["exit_reason"] in ("SESSION_END", "DATA_END") and not nxt_open:
                # closed at the close of this bar (the last of the day or of the data)
                state, ch = ratchet.advance(plan, state, bh, bl, rp)
                mine += self.ev_of(ts, ch, state)
                exit_level = bc
                k_bar = j
                break
            state, ch = ratchet.advance(plan, state, bh, bl, rp)
            mine += self.ev_of(ts, ch, state)
        else:
            P.check(False, f"{w}: the replay never reached the close at {close['ts']}")
        same = len(mine) == len(theirs) and all(
            a[:2] == b[:2] and (a[2] == b[2] if isinstance(a[2], str) else abs(a[2] - b[2]) < 0.011)
            for a, b in zip(mine, theirs, strict=True)
        )
        P.check(same, f"{w}: ratchet events differ\n    replay {mine}\n    engine {theirs}")
        if exit_level is not None:
            if is_opt:
                P.check(
                    abs(t["exit"] - exit_level) < 0.011,
                    f"{w}: exit level {t['exit']} != replay {exit_level} ({t['exit_reason']})",
                )
            else:
                slipped = t["exit_reason"] not in ("TARGET", "GAP_TARGET")
                want = exit_level * (1 - side * SLIP) if slipped else exit_level
                P.check(
                    abs(t["exit_price"] - want) < 0.011,
                    f"{w}: stock exit {t['exit_price']} != {want:.3f}",
                )
        # stage and levels at the close
        P.check(t["stage"] in ratchet.STAGES, f"{w}: stage {t['stage']}")
        P.check(
            abs(t["stop"] - state.stop) < 0.011 or t["exit_reason"] in ("SIGNAL_EXIT", "MANUAL"),
            f"{w}: final stop {t['stop']} vs replay {state.stop}",
        )
        # money
        self.audit_money(w, t, det, is_opt, side, i0, k_bar)
        # the first/last wait and premium source
        if is_opt:
            P.check(
                det.get("entry_source") in ("real", "model"),
                f"{w}: entry source {det.get('entry_source')}",
            )
            P.check(
                det.get("exit_source") in ("real", "model"),
                f"{w}: exit source {det.get('exit_source')}",
            )
            if det.get("entry_source") != det.get("exit_source"):
                P.check(
                    "note" in det and det["note"] in (t["traded"].get("note") or ""),
                    f"{w}: mixed sources without a note",
                )
        return out

    def ev_of(
        self, ts: int, changes: tuple[str, ...], st: ratchet.State
    ) -> list[tuple[int, str, Any]]:
        out = []
        for ch in changes:
            if ch in ("BREAKEVEN", "LOCKED", "TRAILING"):
                out.append((ts + 60, "STAGE", ch))
            elif ch == "STOP_MOVED":
                out.append((ts + 60, "STOP_MOVED", round(st.stop, 2)))
            elif ch == "TARGET_MOVED":
                out.append((ts + 60, "TARGET_MOVED", round(st.target, 2)))
        return out

    def audit_money(
        self,
        w: str,
        t: dict[str, Any],
        det: dict[str, Any],
        is_opt: bool,
        side: int,
        i0: int,
        k_bar: int | None,
    ) -> None:
        qty = t["qty"]
        gross = (t["exit_price"] - t["entry_price"]) * (1 if is_opt else side) * qty
        P.check(abs(gross - t["gross"]) < 0.0101, f"{w}: gross {t['gross']} != {gross:.2f}")
        cm = self.w.option_costs if is_opt else self.w.stock_costs
        if is_opt:
            ch = (
                cm.leg(t["entry_price"], qty, "buy").total
                + cm.leg(t["exit_price"], qty, "sell").total
            )
        else:
            a, b = ("buy", "sell") if side == 1 else ("sell", "buy")
            ch = cm.leg(t["entry_price"], qty, a).total + cm.leg(t["exit_price"], qty, b).total
        P.check(abs(ch - t["charges"]) < 0.0101, f"{w}: charges {t['charges']} != {ch:.2f}")
        P.check(
            abs(round(t["gross"], 2) - round(t["charges"], 2) - t["net"]) < 0.0101,
            f"{w}: net {t['net']} != gross - charges",
        )
        risk_inr = det.get("risk_inr")
        if risk_inr:
            P.check(
                abs(t["net"] / risk_inr - t["r_multiple"]) < 0.0006,
                f"{w}: r_multiple {t['r_multiple']} != net/risk {t['net'] / risk_inr:.4f}",
            )
            if not is_opt:
                P.check(
                    abs(risk_inr - abs(t["entry"] - t["stop0"]) * qty) < 0.02,
                    f"{w}: stock risk_inr",
                )
            else:
                # the first risk in rupees lies on one curve with the real premium: an option
                # worth 0.3 to 1.0 of the index move (delta) per index point of risk
                idx_risk = abs(t["entry"] - t["stop0"]) * qty
                P.check(
                    0.3 * idx_risk <= risk_inr <= 1.0 * idx_risk,
                    f"{w}: option risk_inr {risk_inr:.0f} is {risk_inr / idx_risk:.2f} x the index risk {idx_risk:.0f}",
                )
                self.iv_sources[det.get("iv_source")] += 1
        else:
            P.check(t["r_multiple"] is None, f"{w}: r_multiple without a risk")
        if is_opt:
            self.audit_option_prices(w, t, det, side, i0, k_bar)

    def audit_option_prices(
        self, w: str, t: dict[str, Any], det: dict[str, Any], side: int, i0: int, k_bar: int | None
    ) -> None:
        stored = self.w.stored(self.key)
        pr = self.w.pricer(self.key)
        c = Contract.from_dict(det["contract"])
        f0 = pd.Timestamp(self.minutes[i0][0], unit="s", tz="UTC")
        raw = stored.bar(c, f0) if stored is not None else None
        if raw is not None:
            P.check(
                det.get("entry_source") == "real",
                f"{w}: the store has the bar but the entry says {det.get('entry_source')}",
            )
            P.check(
                abs(t["entry_price"] - pr.option_entry(float(raw[0]))) < 0.0051,
                f"{w}: entry premium {t['entry_price']} != open {raw[0]} with spread and ticks {pr.option_entry(float(raw[0]))}",
            )
        else:
            P.check(
                det.get("entry_source") == "model",
                f"{w}: no stored bar yet the entry says {det.get('entry_source')}",
            )
        P.check(
            abs(round(t["entry_price"] / 0.05) * 0.05 - t["entry_price"]) < 1e-6,
            f"{w}: entry premium not on the 0.05 tick",
        )
        P.check(
            abs(round(t["exit_price"] / 0.05) * 0.05 - t["exit_price"]) < 1e-6,
            f"{w}: exit premium not on the 0.05 tick",
        )
        # exit premium when real
        if det.get("exit_source") == "real" and k_bar is not None:
            ts, bo, bh, bl, bc = self.minutes[k_bar]
            minute = pd.Timestamp(ts, unit="s", tz="UTC")
            reason = t["exit_reason"]
            at_open = pd.Timestamp(t["exit_ts"]).value // 10**9 == ts
            how = (
                "open"
                if at_open
                else ("close" if reason in ("SESSION_END", "DATA_END") else "touch")
            )
            g2 = stored.bar(c, minute) if stored else None
            if P.check(g2 is not None, f"{w}: exit says real but no stored bar at {ts_iso(ts)}"):
                row = g2
                if how == "open":
                    rawx = float(row[0])
                elif how == "close":
                    rawx = float(row[3])
                else:
                    rawx = interpolate(
                        (float(row[0]), float(row[1]), float(row[2]), float(row[3])),
                        (bo, bh, bl, bc),
                        float(t["exit"]),
                        c.opt,
                    )
                P.check(
                    abs(t["exit_price"] - pr.option_exit(rawx)) < 0.0051,
                    f"{w}: exit premium {t['exit_price']} != {pr.option_exit(rawx)} ({how})",
                )


def csv_vs_json(api: Api, sid: str) -> None:
    st, js = api.get(f"/api/sim/trades?session_id={sid}&limit=5000")
    st2, text = api.get(f"/api/sim/trades.csv?session_id={sid}")
    P.check(st == 200 and st2 == 200, "trades endpoints status")
    rows = list(csv.DictReader(io.StringIO(text)))
    P.check(
        len(rows) == len(js["trades"]), f"{sid}: csv has {len(rows)} rows, json {len(js['trades'])}"
    )
    P.check(
        {int(r["id"]) for r in rows} == {t["id"] for t in js["trades"]},
        f"{sid}: csv and json list different trades",
    )
    for r in rows:
        j = next(t for t in js["trades"] if t["id"] == int(r["id"]))
        P.check(
            abs(float(r["net"]) - j["net"]) < 0.006 and r["exit_reason"] == j["exit_reason"],
            f"{sid}: csv row {r['id']} differs",
        )
    P.check(all(t["session_id"] == sid for t in js["trades"]), f"{sid}: trades of another session")
    P.check(
        all(k in js["trades"][0] for k in ("instrument", "strategy", "timeframe", "mode"))
        if js["trades"]
        else True,
        f"{sid}: trades lack instrument/strategy keys",
    )
    # newest first
    ex = [t["exit_ts"] for t in js["trades"]]
    P.check(ex == sorted(ex, reverse=True), f"{sid}: trades are not newest first")


# ── the plan ──────────────────────────────────────────────────────────────────────────────
def plan() -> list[tuple[str, dict[str, Any], int]]:
    def b(inst: str, strat: str, tf: str, day: str) -> dict[str, Any]:
        return {
            "instrument": inst,
            "strategy": strat,
            "timeframe": tf,
            "mode": "past",
            "start": f"{day}T09:15:00+05:30",
        }

    return [
        (
            "nifty_mt_1110",
            b("NIFTY", "microtrend", "3m", "2026-11-10"),
            4,
        ),  # to the end of the data, past VIX
        ("nifty_mt_1005", b("NIFTY", "microtrend", "3m", "2026-10-05"), 3),
        ("nifty_mt_0915", b("NIFTY", "microtrend", "3m", "2026-09-15"), 3),
        ("nifty_mt_0812", b("NIFTY", "microtrend", "3m", "2026-08-12"), 3),
        ("nifty_mt_0707", b("NIFTY", "microtrend", "3m", "2026-07-07"), 3),
        ("bank_mt_1012", b("BANKNIFTY", "microtrend", "3m", "2026-10-12"), 3),
        ("bank_mt_1110", b("BANKNIFTY", "microtrend", "3m", "2026-11-10"), 3),
        ("rel_stv", b("RELIANCE", "supertrend_vol", "15m", "2026-10-26"), 3),
        ("rel_orb", b("RELIANCE", "orb", "15m", "2026-10-26"), 3),
        ("sbin_stv", b("SBIN", "supertrend_vol", "15m", "2026-10-26"), 3),
        ("sbin_orb", b("SBIN", "orb", "15m", "2026-10-26"), 3),
    ]


def long_plan() -> list[tuple[str, dict[str, Any], int]]:
    """Many days, for counts: the options rule on two indices, three stock rules on four shares."""

    def b(inst: str, strat: str, tf: str, day: str) -> dict[str, Any]:
        return {
            "instrument": inst,
            "strategy": strat,
            "timeframe": tf,
            "mode": "past",
            "start": f"{day}T09:15:00+05:30",
        }

    out = [
        ("L_nifty_mt", b("NIFTY", "microtrend", "3m", "2026-05-05"), 120),
        ("L_bank_mt", b("BANKNIFTY", "microtrend", "3m", "2026-05-05"), 120),
        ("L_fin_mt", b("FINNIFTY", "microtrend", "3m", "2026-05-05"), 120),
        ("L_nifty_ema", b("NIFTY", "ema_pullback", "15m", "2026-05-05"), 120),
    ]
    for inst in ("RELIANCE", "SBIN", "ITC", "TMPV"):
        for strat in ("supertrend_vol", "orb", "ema_cross"):
            out.append((f"L_{inst.lower()}_{strat}", b(inst, strat, "15m", "2026-05-05"), 120))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run", "compare"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--only", default=None)
    ap.add_argument(
        "--long", action="store_true", help="the many-day plan instead of the short one"
    )
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--base", default=BASE)
    a = ap.parse_args()
    if "8787" in a.base:
        print("refusing: that is the owner's desk")
        return 2
    token = (ROOT / "data" / "api_token.txt").read_text().strip()
    api = Api(a.base, token)
    if a.mode == "compare":
        return compare(api, Path(a.file or "/tmp/sim_snapshot.json"))
    world = World()
    runs: list[Run] = []
    audits: list[dict[str, Any]] = []
    for i, (name, body, days) in enumerate(long_plan() if a.long else plan()):
        if a.only and a.only not in name:
            continue
        print(
            f"== {name}: {body['instrument']} {body['strategy']} {body['timeframe']} from {body['start'][:10]} for {days} day(s)"
        )
        r = Run(api, world, name, body, days, seed=100 + i)
        try:
            r.drive()
        except Exception as exc:  # noqa: BLE001
            P.check(False, f"{name}: crashed: {exc!r}")
            continue
        rows = r.audit()
        csv_vs_json(api, r.sid)
        audits += rows
        runs.append(r)
        g = r.final
        print(
            f"   alerts {r.alerts_seen}  taken {r.taken}  closed {len(g['trades'])}  net {g['summary']['net']}  day_ends {r.day_ends}  over {r.over}  create {r.created_ms:.0f} ms  1-min advance median {statistics.median(r.advance_ms) if r.advance_ms else 0:.1f} ms  max {max(r.advance_ms) if r.advance_ms else 0:.1f} ms"
        )
        print(f"   note: {r.premium_note}")
    # totals
    opt = [x for x in audits if x["opt"]]
    print("\n== totals")
    print(
        f"sessions {len(runs)}  alerts {sum(r.alerts_seen for r in runs)}  taken {sum(r.taken for r in runs)}  closed trades {len(audits)}"
    )
    print(
        "option trades:",
        len(opt),
        " entry",
        dict(Counter(x["entry_source"] for x in opt)),
        " exit",
        dict(Counter(x["exit_source"] for x in opt)),
    )
    print("iv source", dict(sum((r.iv_sources for r in runs), Counter())))
    print("exit reasons", dict(Counter(x["reason"] for x in audits)))
    allms = [m for r in runs for m in r.advance_ms]
    if allms:
        print(
            f"1-minute advance: median {statistics.median(allms):.1f} ms  p95 {sorted(allms)[int(len(allms) * 0.95)]:.1f} ms  max {max(allms):.1f} ms  (n={len(allms)})"
        )
    print(f"session creation ms: {[round(r.created_ms) for r in runs]}")
    # timing of a whole-day advance on fresh sessions
    for inst, strat, tf in (("NIFTY", "microtrend", "3m"), ("RELIANCE", "supertrend_vol", "15m")):
        body = {
            "instrument": inst,
            "strategy": strat,
            "timeframe": tf,
            "mode": "past",
            "start": "2026-10-01T09:15:00+05:30",
        }
        st, out = api.post("/api/sim/sessions", body)
        t = time.perf_counter()
        st, r = api.post(f"/api/sim/sessions/{out['session']['id']}/advance", {"minutes": 390})
        print(
            f"advance(390) {inst}: {(time.perf_counter() - t) * 1000:.0f} ms, minutes {len(r['minutes'])}, events {len(r['events'])}"
        )
    print(f"\nchecks {P.checks}  problems {len(P.items)}  http calls {api.calls}")
    if a.snapshot:
        snap = {r.sid: r.get() for r in runs}
        Path(a.snapshot).write_text(json.dumps(snap))
        print("snapshot ->", a.snapshot)
    for p in P.items[60:]:
        print("  PROBLEM:", p)
    return 1 if P.items else 0


def compare(api: Api, path: Path) -> int:
    snap = json.loads(path.read_text())
    for sid, old in snap.items():
        st, new = api.get(f"/api/sim/sessions/{sid}")
        P.check(st == 200, f"{sid}: GET after restart -> {st}")
        if st != 200:
            continue
        for k in ("session", "alert", "summary", "trades", "candles"):
            P.check(new[k] == old[k], f"{sid}: {k} differs after the restart")
        if new["trade"] != old["trade"]:
            P.check(
                new["trade"] is not None
                and old["trade"] is not None
                and all(
                    new["trade"][k] == old["trade"][k]
                    for k in ("id", "status", "stop", "target", "stage", "entry_price")
                ),
                f"{sid}: open trade differs",
            )
    print(f"restart: sessions {len(snap)}  checks {P.checks}  problems {len(P.items)}")
    return 1 if P.items else 0


if __name__ == "__main__":
    sys.exit(main())
