"""What a one-unit fill really costs, measured from the broker's own quotes. Never an order.

The owner asked (2026-11-07) to measure the actual cost of filling ONE share or ONE lot, the
assumption every backtest rests on: 0.03 % slippage per side on stocks, and half-spread
0.6 % of the premium plus 2 ticks per side on options. Read-only quotes measure it:

* spread: a market BUY pays the ask and a SELL gets the bid, so one side costs half the
  quoted spread against the mid. Sampled every `sample_every` seconds of the session for
  every watched stock and, per index, the ~0.65-delta monthly CE and PE an alert would name
  (their ids from the option chain, refreshed every `chain_every` seconds);
* the alert: the alerted instrument's quote when the alert is raised (ALERT), then 1, 2 and
  5 minutes later (FOLLOW): what waiting for a manual fill costs, signed so a positive
  number is a worse price for that alert's side;
* the owner's own fills: the prices typed on a taken alert, against the alert's prices.

Everything runs on its own thread, so a slow quote never delays the desk's poll.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Engine, insert, select

from ..broker.base import Contract
from ..clock import IST, now_ist
from ..store.schema import METADATA, desk_positions, desk_signals, fill_quotes
from .rules import pick_expiry

log = logging.getLogger(__name__)

FOLLOW_OFFSETS = (60, 120, 300)  # seconds after an alert
LAB_STOCK_SLIPPAGE_PCT = 0.03  # per side, BacktestCfg.slippage_pct
BUCKETS = (("09:15", "10:00"), ("10:00", "11:30"), ("11:30", "13:00"), ("13:00", "15:30"))


@dataclass(frozen=True)
class Watched:
    """One instrument whose quote is sampled."""

    instrument_key: str
    security_id: str
    segment: str
    label: str
    instrument_type: str  # EQUITY | OPTIDX

    def contract(self) -> Contract:
        return Contract(
            security_id=self.security_id,
            exchange_segment=self.segment,
            symbol=self.label,
            underlying=self.instrument_key,
            instrument_type=self.instrument_type,
        )


def half_spread_pct(bid: float | None, ask: float | None) -> float | None:
    """Half the quoted spread as % of the mid: one side's cost of a market order. None for
    a missing, one-sided or crossed quote."""
    if not _pos(bid) or not _pos(ask) or ask < bid:  # type: ignore[operator]
        return None
    mid = (bid + ask) / 2  # type: ignore[operator]
    return (ask - bid) / 2 / mid * 100  # type: ignore[operator]


def exec_price(side: str, bid: float | None, ask: float | None, ltp: float | None) -> float | None:
    """What a one-unit market order would fill at: the ask for a BUY, the bid for a SELL,
    the last price when that side of the book is empty."""
    px = ask if side.startswith("BUY") else bid
    return float(px) if _pos(px) else (float(ltp) if _pos(ltp) else None)  # type: ignore[arg-type]


def adverse_pct(side: str, base: float, later: float) -> float:
    """How much worse `later` is than `base` for this side, in % (positive = worse)."""
    sign = 1 if side.startswith("BUY") else -1
    return (later - base) / base * 100 * sign


def _pos(v: Any) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0


class FillCostRecorder:
    """Samples quotes on its own thread; `on_alert` only enqueues."""

    def __init__(
        self,
        engine: Engine,
        adapter_getter: Callable[[], Any],
        data_api_active: Callable[[], bool],
        market_open: Callable[[dt.datetime], bool],
        stocks: Callable[[], Iterable[Watched]],
        indices: Callable[[], Iterable[tuple[str, str, str, str]]],  # key, sid, exchange, inst
        sample_every: int = 300,
        chain_every: int = 1800,
        roll_days: int = 3,
        target_delta: float = 0.65,
        clock: Callable[[], dt.datetime] = now_ist,
    ) -> None:
        self.engine = engine
        self._adapter = adapter_getter
        self._active = data_api_active
        self._open = market_open
        self._stocks = stocks
        self._indices = indices
        self.sample_every = sample_every
        self.chain_every = chain_every
        self.roll_days = roll_days
        self.target_delta = target_delta
        self.clock = clock
        self._alerts: deque[dict] = deque(maxlen=200)
        self._follow: list[tuple[dt.datetime, dict, Watched, int]] = []
        self._options: dict[str, Watched] = {}  # by security id
        self._last_sample: dt.datetime | None = None
        self._last_chain: dt.datetime | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors = 0
        METADATA.create_all(engine, tables=[fill_quotes])

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="fill-costs", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:  # pragma: no cover - timing; tick() is tested
        while not self._stop.is_set():
            try:
                self.tick(self.clock())
            except Exception:  # noqa: BLE001
                self.errors += 1
                log.exception("fill-cost sampling failed")
            self._stop.wait(10)

    # ── inputs ────────────────────────────────────────────────────────────
    def on_alert(self, sig: dict) -> None:
        """Queue an alert's instrument for an immediate quote and the follow-ups."""
        with self._lock:
            self._alerts.append(dict(sig))

    # ── one tick ──────────────────────────────────────────────────────────
    def tick(self, now: dt.datetime) -> int:
        """Quote what is due; returns rows written."""
        adapter = self._adapter()
        if adapter is None or not self._active():
            return 0
        with self._lock:
            alerts = list(self._alerts)
            self._alerts.clear()
            due = [f for f in self._follow if f[0] <= now]
            self._follow = [f for f in self._follow if f[0] > now]
        rows: list[dict] = []
        asks: list[tuple[str, dict, Watched, int | None]] = []
        for sig in alerts:
            w = self._alert_watched(sig)
            if w is not None:
                asks.append(("ALERT", sig, w, None))
        asks += [("FOLLOW", sig, w, off) for _, sig, w, off in due]
        last_sample = self._last_sample
        sampling = self._open(now) and (
            last_sample is None or (now - last_sample).total_seconds() >= self.sample_every
        )
        if sampling:
            self._refresh_options(adapter, now)
            self._last_sample = now
            for w in [*self._stocks(), *self._options.values()]:
                asks.append(("SAMPLE", {}, w, None))
        if not asks:
            return 0
        try:
            quotes = adapter.quote([w.contract() for _, _, w, _ in asks])
        except Exception:
            # nothing is lost to one broker error: the alerts and the follow-ups that were
            # due go back in the queue for the next tick, and the sample is due again
            with self._lock:
                self._alerts.extendleft(reversed(alerts))
                self._follow.extend(due)
            self._last_sample = last_sample
            raise
        with self._lock:  # an alert's follow-ups start once its first quote was asked
            for kind, sig, w, _ in asks:
                if kind == "ALERT":
                    for delay in FOLLOW_OFFSETS:
                        self._follow.append((now + dt.timedelta(seconds=delay), sig, w, delay))
        for kind, sig, w, off in asks:
            q = quotes.get(str(w.security_id))
            if q is None:
                continue
            rows.append(
                {
                    "ts": now,
                    "kind": kind,
                    "instrument_key": w.instrument_key,
                    "security_id": w.security_id,
                    "segment": w.segment,
                    "label": w.label,
                    "alert_id": sig.get("id"),
                    "side": sig.get("side"),
                    "offset_sec": off,
                    "ltp": q.ltp,
                    "bid": q.bid,
                    "ask": q.ask,
                    "bid_qty": q.bid_qty,
                    "ask_qty": q.ask_qty,
                }
            )
        if rows:
            with self.engine.begin() as conn:
                conn.execute(insert(fill_quotes), rows)
        return len(rows)

    def _alert_watched(self, sig: dict) -> Watched | None:
        eco = sig.get("economics") or {}
        opt = eco.get("option")
        if isinstance(opt, dict) and opt.get("security_id"):
            seg = opt.get("segment") or "NSE_FNO"
            label = f"{sig.get('series_key')} {opt.get('strike'):g} {opt.get('opt_type')} {opt.get('expiry')}"
            return Watched(
                str(sig.get("instrument_key")), str(opt["security_id"]), seg, label, "OPTIDX"
            )
        for w in self._stocks():
            if w.instrument_key == sig.get("instrument_key"):
                return w
        return None  # a synthetic option (no contract id) has no quote to take

    def _refresh_options(self, adapter: Any, now: dt.datetime) -> None:
        """The ~target-delta monthly CE and PE of each index, from its option chain."""
        if self._last_chain is not None and (
            (now - self._last_chain).total_seconds() < self.chain_every
        ):
            return
        self._last_chain = now
        found: dict[str, Watched] = {}
        for key, sid, exchange, inst in self._indices():
            try:
                expiry = pick_expiry(
                    adapter.expiry_list_raw(int(sid), "IDX_I") or [],
                    now.date(),
                    "monthly",
                    self.roll_days,
                )
                if expiry is None:
                    continue
                chain = adapter.option_chain(key, expiry, int(sid), "IDX_I")
            except Exception as exc:  # noqa: BLE001 - one index never stops the rest
                log.debug("fill costs: no chain for %s: %s", key, exc)
                continue
            for opt in ("CE", "PE"):
                rows = [
                    r
                    for r in getattr(chain, "rows", [])
                    if r.opt_type == opt and r.delta is not None and r.security_id
                ]
                if not rows:
                    continue
                best = min(rows, key=lambda r: abs(abs(r.delta) - self.target_delta))
                found[str(best.security_id)] = Watched(
                    inst,
                    str(best.security_id),
                    f"{exchange}_FNO",
                    f"{key} {best.strike:g} {opt} {expiry}",
                    "OPTIDX",
                )
        if found:
            self._options = found


# ── the report ───────────────────────────────────────────────────────────────────────────
def _bucket(ts: dt.datetime) -> str:
    hm = ts.astimezone(IST).strftime("%H:%M")
    for lo, hi in BUCKETS:
        if lo <= hm < hi:
            return f"{lo}-{hi}"
    return "other"


def _q(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def report(engine: Engine, days: int = 30, now: dt.datetime | None = None) -> dict:
    """Measured one-unit fill costs over the last `days`, beside the Lab's assumptions."""
    now = now or now_ist()
    since = now - dt.timedelta(days=days)
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(select(fill_quotes).where(fill_quotes.c.ts >= since))
            .mappings()
            .all()
        ]
        sigs = {
            r["id"]: r["payload"] or {}
            for r in conn.execute(
                select(desk_signals.c.id, desk_signals.c.payload).where(
                    desk_signals.c.created_at >= since
                )
            )
            .mappings()
            .all()
        }
        taken = [
            dict(r["payload"] or {})
            for r in conn.execute(
                select(desk_positions.c.payload).where(desk_positions.c.opened_at >= since)
            )
            .mappings()
            .all()
        ]
    spreads: dict[str, dict] = {}
    for r in rows:
        if r["kind"] != "SAMPLE":
            continue
        hs = half_spread_pct(r["bid"], r["ask"])
        if hs is None:
            continue
        kind = "option" if r["segment"] and r["segment"].endswith("_FNO") else "stock"
        name = (
            r["instrument_key"]
            if kind == "stock"
            else f"{r['instrument_key']} {r['label'].split()[-2]}"
        )
        e = spreads.setdefault(name, {"kind": kind, "all": [], "buckets": {}})
        e["all"].append(hs)
        e["buckets"].setdefault(_bucket(r["ts"]), []).append(hs)
    by_instrument = [
        {
            "instrument": k,
            "kind": v["kind"],
            "samples": len(v["all"]),
            "half_spread_median_pct": _q(v["all"], 0.5),
            "half_spread_p90_pct": _q(v["all"], 0.9),
            "by_time": {b: _q(x, 0.5) for b, x in sorted(v["buckets"].items())},
        }
        for k, v in sorted(spreads.items())
    ]
    alerts: list[dict] = []
    by_alert: dict[str, list[dict]] = {}
    for r in rows:
        if r["kind"] in ("ALERT", "FOLLOW") and r["alert_id"]:
            by_alert.setdefault(r["alert_id"], []).append(r)
    for aid, rs in by_alert.items():
        base = next((r for r in rs if r["kind"] == "ALERT"), None)
        if base is None or not base["side"]:
            continue
        side = str(base["side"])
        px0 = exec_price(side, base["bid"], base["ask"], base["ltp"])
        if px0 is None:
            continue
        sig = sigs.get(aid, {})
        eco = sig.get("economics") or {}
        alert_px = eco.get("entry_price")  # per unit: the share price or the option premium
        out = {
            "alert_id": aid,
            "instrument": base["label"] or base["instrument_key"],
            "side": side,
            "at": base["ts"].astimezone(IST).isoformat() if base["ts"] else None,
            "half_spread_pct": half_spread_pct(base["bid"], base["ask"]),
            "fill_vs_alert_pct": adverse_pct(side, float(cast(float, alert_px)), px0)
            if _pos(alert_px)
            else None,
        }
        for r in rs:
            if r["kind"] == "FOLLOW" and r["offset_sec"]:
                later = exec_price(side, r["bid"], r["ask"], r["ltp"])
                if later is not None:
                    out[f"after_{r['offset_sec']}s_pct"] = adverse_pct(side, px0, later)
        alerts.append(out)
    owner = []
    for p in taken:
        for leg, mine, theirs in (
            (
                "entry",
                p.get("user_entry_price"),
                p.get("alert_entry_price") or p.get("entry_price"),
            ),
            ("exit", p.get("user_exit_price"), p.get("alert_exit_price")),
        ):
            if _pos(mine) and _pos(theirs):
                side = str(p.get("side") or "BUY")
                if leg == "exit":  # the exit is the opposite order
                    side = "SELL" if side.startswith("BUY") else "BUY"
                owner.append(
                    {
                        "position": p.get("id"),
                        "instrument": p.get("instrument_key"),
                        "leg": leg,
                        "alert_price": theirs,
                        "your_price": mine,
                        "slippage_pct": adverse_pct(
                            side, float(cast(float, theirs)), float(cast(float, mine))
                        ),
                    }
                )
    stock_med = [x["half_spread_median_pct"] for x in by_instrument if x["kind"] == "stock"]
    opt_med = [x["half_spread_median_pct"] for x in by_instrument if x["kind"] == "option"]
    return {
        "days": days,
        "samples": sum(1 for r in rows if r["kind"] == "SAMPLE"),
        "lab_assumptions": {
            "stock_slippage_pct_per_side": LAB_STOCK_SLIPPAGE_PCT,
            "option_half_spread_pct_per_side": 0.6,
            "option_slippage_ticks_per_side": 2,
        },
        "measured": {
            "stock_half_spread_median_pct": statistics.median(stock_med) if stock_med else None,
            "option_half_spread_median_pct": statistics.median(opt_med) if opt_med else None,
        },
        "by_instrument": by_instrument,
        "alerts": alerts,
        "owner_fills": owner,
        "note": "quotes only (bid/ask/last), never an order; positive = worse for the trader",
    }


# ── charges: what Dhan billed per fill against the cost table ───────────────────────────────
CHARGE_FIELDS = (
    "brokerageCharges",
    "stt",
    "exchangeTransactionCharges",
    "sebiTax",
    "stampDuty",
    "serviceTax",
)
_FNO_PRODUCTS = {
    "OPTIDX": "index_options",
    "OPTSTK": "stock_options",
    "FUTIDX": "index_futures",
    "FUTSTK": "stock_futures",
}


def cost_product(trade: dict) -> str | None:
    """The costs.yaml product a Dhan trade-book row is charged as, or None if not modelled."""
    seg = str(trade.get("exchangeSegment") or "")
    if seg.endswith("_EQ"):
        return "equity_intraday" if trade.get("productType") == "INTRADAY" else "equity_delivery"
    if seg.endswith("_FNO"):
        return _FNO_PRODUCTS.get(str(trade.get("instrument") or ""))
    if seg.endswith("_CURRENCY"):
        kind = str(trade.get("drvOptionType") or "").upper()
        return "currency_options" if kind in ("CALL", "PUT", "CE", "PE") else "currency_futures"
    return None


def _money(v: Any) -> float:
    """A charge field as a finite number (a missing or unusable one counts as 0)."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def charges_check(trades: Iterable[dict], cost_for: Callable[[str], Any]) -> list[dict]:
    """Dhan's billed charges per fill (trade history) beside the cost table's charges for the
    same price, quantity and side, per (product, side). `cost_for(product)` -> CostModel."""
    groups: dict[tuple[str, str], dict] = {}
    for r in trades:
        product = cost_product(r)
        side = str(r.get("transactionType") or "").upper()
        try:
            qty_f, px = float(r["tradedQuantity"]), float(r["tradedPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(qty_f) and math.isfinite(px)) or qty_f <= 0 or px <= 0:
            continue
        if qty_f != int(qty_f):  # a fill is a whole number of units
            continue
        qty = int(qty_f)
        if product is None or side not in ("BUY", "SELL") or qty <= 0:
            continue
        actual = sum(_money(r.get(k)) for k in CHARGE_FIELDS)
        model = float(cost_for(product).leg(px, qty, side.lower()).total)
        g = groups.setdefault(
            (product, side),
            {"product": product, "side": side, "fills": 0, "turnover": 0.0, "actual": 0.0,
             "model": 0.0, "actual_parts": dict.fromkeys(CHARGE_FIELDS, 0.0)},
        )  # fmt: skip
        g["fills"] += 1
        g["turnover"] += px * qty
        g["actual"] += actual
        g["model"] += model
        for k in CHARGE_FIELDS:
            g["actual_parts"][k] += _money(r.get(k))
    out = []
    for g in groups.values():
        t = g["turnover"]
        out.append(
            g
            | {
                "actual_pct": g["actual"] / t * 100 if t else None,
                "model_pct": g["model"] / t * 100 if t else None,
                "actual": round(g["actual"], 2),
                "model": round(g["model"], 2),
                "turnover": round(t, 2),
                "actual_parts": {k: round(v, 2) for k, v in g["actual_parts"].items()},
            }
        )
    return sorted(out, key=lambda x: (x["product"], x["side"]))
