"""The instrument universe: price series and the one-unit tradeables built on them.

config/universe.yaml declares SERIES (indices, stocks, spot FX). This module
expands them into INSTRUMENTS the Backtest Lab can trade under the one-lot
rule and keeps a copy in the `universe` table for the UI.

Categories and what "one unit" means:
    STOCK      1 share of the stock (cash, intraday)
    FUTURES    1 lot of the near-month future on an index or a stock
    OPTIONS    1 lot of a delta-selected CE (long) / PE (short) on an index or a stock
    CURRENCY   1 lot of the near-month currency future
    INDEX      the series itself: study only, never traded
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import Engine, select

from ..config import PRODUCT_FOR_CATEGORY
from ..settings import load_universe
from .db import upsert, utcnow
from .schema import universe as universe_table

EQUITY_SESSION = {"open": "09:15", "close": "15:30", "square_off": "15:15",
                  "first_entry": "09:30", "last_entry": "14:45"}
CURRENCY_SESSION = {"open": "09:00", "close": "17:00", "square_off": "16:45",
                    "first_entry": "09:15", "last_entry": "16:00"}


@dataclass(frozen=True)
class SeriesSpec:
    key: str
    label: str
    kind: str                      # INDEX | STOCK | FX
    yahoo: str | None
    dhan_security_id: str | None
    dhan_segment: str              # IDX_I | NSE_EQ | NSE_CURRENCY
    dhan_instrument: str           # INDEX | EQUITY | FUTCUR
    exchange: str = "NSE"
    session: str = "equity"        # equity | currency
    has_volume: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def session_times(self) -> dict:
        return CURRENCY_SESSION if self.session == "currency" else EQUITY_SESSION


@dataclass(frozen=True)
class InstrumentSpec:
    key: str
    label: str
    category: str                  # INDEX | STOCK | FUTURES | OPTIONS | CURRENCY
    series_key: str
    lot_size: int                  # units per trade (1 for a stock)
    tick_size: float
    product: str                   # cost table in costs.yaml
    strike_step: float = 0.0
    session: dict = field(default_factory=lambda: dict(EQUITY_SESSION))
    tradeable: bool = True
    price_per: float = 1.0         # JPYINR quotes per 100 JPY
    extra: dict = field(default_factory=dict)

    @property
    def is_option(self) -> bool:
        return self.category == "OPTIONS"

    @property
    def is_index_derivative(self) -> bool:
        return self.extra.get("underlying_kind") == "INDEX"

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "category": self.category,
            "series_key": self.series_key, "lot_size": self.lot_size,
            "tick_size": self.tick_size, "product": self.product,
            "strike_step": self.strike_step, "session": dict(self.session),
            "tradeable": self.tradeable, "price_per": self.price_per,
            "extra": dict(self.extra),
        }


def _session_for(uni: dict, name: str) -> dict:
    base = dict(CURRENCY_SESSION if name == "currency" else EQUITY_SESSION)
    base.update((uni.get("sessions") or {}).get(name) or {})
    return base


def load_specs(uni: dict | None = None) -> tuple[dict[str, SeriesSpec], dict[str, InstrumentSpec]]:
    uni = uni or load_universe()
    series: dict[str, SeriesSpec] = {}
    instruments: dict[str, InstrumentSpec] = {}
    eq_sess = _session_for(uni, "equity")
    cur_sess = _session_for(uni, "currency")

    for e in uni.get("indices") or []:
        key = str(e["key"]).upper()
        series[key] = SeriesSpec(key, e.get("label", key), "INDEX", e.get("yahoo"),
                                 str(e.get("dhan_security_id") or "") or None, "IDX_I", "INDEX",
                                 e.get("exchange", "NSE"), "equity", has_volume=False,
                                 extra={k: v for k, v in e.items() if k not in ("key", "label", "yahoo")})
        instruments[key] = InstrumentSpec(key, e.get("label", key), "INDEX", key, 1, 0.05,
                                          "index_futures", float(e.get("strike_step") or 0),
                                          dict(eq_sess), tradeable=False,
                                          extra={"underlying_kind": "INDEX"})
        if e.get("fno"):
            lot = int(e.get("lot_size") or 1)
            step = float(e.get("strike_step") or 100)
            seg = "BSE_FNO" if e.get("exchange") == "BSE" else "NSE_FNO"
            instruments[f"{key}_FUT"] = InstrumentSpec(
                f"{key}_FUT", f"{e.get('label', key)} futures (1 lot = {lot})", "FUTURES", key,
                lot, 0.05, PRODUCT_FOR_CATEGORY["FUTURES"], step, dict(eq_sess),
                extra={"underlying_kind": "INDEX", "dhan_segment": seg, "dhan_instrument": "FUTIDX"})
            instruments[f"{key}_OPT"] = InstrumentSpec(
                f"{key}_OPT", f"{e.get('label', key)} options (1 lot = {lot})", "OPTIONS", key,
                lot, 0.05, PRODUCT_FOR_CATEGORY["OPTIONS"], step, dict(eq_sess),
                extra={"underlying_kind": "INDEX", "dhan_segment": seg, "dhan_instrument": "OPTIDX"})

    for e in uni.get("stocks") or []:
        key = str(e["key"]).upper()
        series[key] = SeriesSpec(key, e.get("label", key), "STOCK", e.get("yahoo"),
                                 str(e.get("dhan_security_id") or "") or None, "NSE_EQ", "EQUITY",
                                 "NSE", "equity", has_volume=True,
                                 extra={k: v for k, v in e.items() if k not in ("key", "label", "yahoo")})
        instruments[key] = InstrumentSpec(key, f"{e.get('label', key)} (1 share)", "STOCK", key, 1,
                                          0.05, PRODUCT_FOR_CATEGORY["STOCK"], 0.0, dict(eq_sess),
                                          extra={"underlying_kind": "STOCK", "sector": e.get("sector")})
        if e.get("fno"):
            lot = int(e.get("lot_size") or 1)
            step = float(e.get("strike_step") or 10)
            instruments[f"{key}_FUT"] = InstrumentSpec(
                f"{key}_FUT", f"{e.get('label', key)} futures (1 lot = {lot})", "FUTURES", key,
                lot, 0.05, PRODUCT_FOR_CATEGORY["STOCK_FUTURES"], step, dict(eq_sess),
                extra={"underlying_kind": "STOCK", "dhan_segment": "NSE_FNO", "dhan_instrument": "FUTSTK"})
            instruments[f"{key}_OPT"] = InstrumentSpec(
                f"{key}_OPT", f"{e.get('label', key)} options (1 lot = {lot})", "OPTIONS", key,
                lot, 0.05, PRODUCT_FOR_CATEGORY["STOCK_OPTIONS"], step, dict(eq_sess),
                extra={"underlying_kind": "STOCK", "dhan_segment": "NSE_FNO", "dhan_instrument": "OPTSTK"})

    for e in uni.get("currency") or []:
        key = str(e["key"]).upper()
        series[key] = SeriesSpec(key, e.get("label", key), "FX", e.get("yahoo"),
                                 str(e.get("dhan_security_id") or "") or None, "NSE_CURRENCY", "FUTCUR",
                                 "NSE", "currency", has_volume=False,
                                 extra={k: v for k, v in e.items() if k not in ("key", "label", "yahoo")})
        lot = int(e.get("lot_size") or 1000)
        instruments[key] = InstrumentSpec(
            key, f"{e.get('label', key)} futures (1 lot = {lot})", "CURRENCY", key, lot,
            float(e.get("tick_size") or 0.0025), PRODUCT_FOR_CATEGORY["CURRENCY"], 0.0, dict(cur_sess),
            price_per=float(e.get("price_per") or 1.0),
            extra={"underlying_kind": "FX", "dhan_segment": "NSE_CURRENCY", "dhan_instrument": "FUTCUR",
                   "quote_ccy": e.get("quote_ccy", "INR")})
    return series, instruments


def market_index_key(uni: dict | None = None) -> str:
    uni = uni or load_universe()
    return str(uni.get("market_index") or "NIFTY").upper()


def tradeable_instruments(instruments: dict[str, InstrumentSpec] | None = None) -> list[InstrumentSpec]:
    instruments = instruments or load_specs()[1]
    order = {"OPTIONS": 0, "FUTURES": 1, "STOCK": 2, "CURRENCY": 3, "INDEX": 4}
    return sorted([i for i in instruments.values() if i.tradeable],
                  key=lambda i: (order.get(i.category, 9), i.key))


def sync_universe_table(engine: Engine) -> int:
    """Mirror the expanded universe into the `universe` table (for the UI and for
    joins against bars). Idempotent."""
    series, instruments = load_specs()
    rows = []
    now = utcnow()
    for inst in instruments.values():
        s = series.get(inst.series_key)
        rows.append({
            "key": inst.key, "label": inst.label, "category": inst.category,
            "series_key": inst.series_key, "yahoo_symbol": s.yahoo if s else None,
            "dhan_security_id": s.dhan_security_id if s else None,
            "dhan_segment": inst.extra.get("dhan_segment") or (s.dhan_segment if s else None),
            "dhan_instrument": inst.extra.get("dhan_instrument") or (s.dhan_instrument if s else None),
            "lot_size": inst.lot_size, "tick_size": inst.tick_size, "product": inst.product,
            "session_open": inst.session["open"], "session_close": inst.session["close"],
            "square_off": inst.session["square_off"], "in_universe": inst.tradeable,
            "extra": {**inst.extra, "strike_step": inst.strike_step, "price_per": inst.price_per},
            "updated_at": now,
        })
    return upsert(engine, universe_table, rows, ["key"])


def universe_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(universe_table).order_by(universe_table.c.category,
                                                            universe_table.c.key)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("updated_at"), dt.datetime):
            d["updated_at"] = d["updated_at"].isoformat()
        out.append(d)
    return out
