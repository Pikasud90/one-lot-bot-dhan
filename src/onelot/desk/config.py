"""config/desk.yaml -> typed desk configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..settings import PROJECT_ROOT

if TYPE_CHECKING:
    from ..paper.ledger import Slot


@dataclass(frozen=True)
class StrategySlotCfg:
    name: str
    timeframe: str = "5m"
    params: dict = field(default_factory=dict)
    # US-067 D9: a paused slot (enabled: false) raises no alert and is not evaluated, except
    # while a trade it opened is still active (D16); its scoreboard row stays, shown as paused.
    enabled: bool = True
    # "intraday" (the tracker's strategies) or "delivery" (the pullback rule, `onelot.delivery`)
    product: str = "intraday"


@dataclass(frozen=True)
class ContextCfg:
    key: str
    label: str
    manual: bool = False
    note: str = ""


@dataclass(frozen=True)
class DeskInstrumentCfg:
    key: str  # series key (BANKNIFTY, RELIANCE, MM ...)
    tracked: str  # instrument key the signal is expressed in (BANKNIFTY_OPT, RELIANCE)
    kind: str  # INDEX | STOCK
    strategies: tuple[StrategySlotCfg, ...]
    note: str = ""


@dataclass(frozen=True)
class DeskConfig:
    poll_seconds: int = 60
    history_days: int = 15
    source: str = "auto"
    signal_valid_minutes: int = 10
    stock_horizon: str = (
        "swing"  # swing = a stock position may be carried for days; intraday = square off
    )
    swing_max_sessions: int = 5  # time stop for swing positions, in sessions
    audit_days: int = 7  # archived alerts re-audited against later bars for this long
    context: tuple[ContextCfg, ...] = ()
    indices: tuple[DeskInstrumentCfg, ...] = ()
    stocks: tuple[DeskInstrumentCfg, ...] = ()
    path: str = ""
    primary: tuple[str, ...] = ()  # desk.yaml primary_strategies: the owner's order
    # The option an index alert names (paper test, 2026-11-07): "monthly" = the Lab's
    # contract (the month's last expiry with at least `option_roll_days` days left);
    # "nearest" = the first listed expiry (a weekly where one exists).
    option_expiry: str = "monthly"
    option_roll_days: int = 3

    def _owner_order(self) -> tuple[str, ...]:
        """desk.yaml `primary_strategies` in the owner's order, then every other configured
        strategy in configuration order, each once."""
        seen: dict[str, None] = dict.fromkeys(self.primary)
        for inst in self.instruments:
            for slot in inst.strategies:
                seen.setdefault(slot.name, None)
        return tuple(seen)

    def primary_strategies(self) -> tuple[str, ...]:
        """The owner's primary strategies that are Lab strategies: desk.yaml
        `primary_strategies` in the owner's order, else every strategy the desk runs, in
        configuration order, each once. The Lab's 'primary strategies' filter shows them. A
        delivery rule (US-067 `pullback`) is not a Lab strategy and is left out here; the Home
        scoreboard lists it through `primary_slots`."""
        delivery = {
            s.name for i in self.instruments for s in i.strategies if s.product == "delivery"
        }
        intraday = {
            s.name for i in self.instruments for s in i.strategies if s.product != "delivery"
        }
        return tuple(n for n in self._owner_order() if n not in delivery - intraday)

    def primary_slots(self) -> tuple[Slot, ...]:
        """The primary strategies as the desk runs them (the paper test, 2026-11-08): one
        (name, timeframe, market) per configured slot of a primary strategy, in the owner's
        strategy order, then configuration order, each once. The Home scoreboard counts each
        apart, so an earlier timeframe of the same strategy never counts toward it."""
        from ..paper.ledger import Slot  # here: the paper layer stays out of config imports

        order = {name: i for i, name in enumerate(self._owner_order())}
        seen: dict[Slot, None] = {}
        for inst in self.instruments:
            for slot in inst.strategies:
                if slot.name in order:
                    seen.setdefault(Slot(slot.name, slot.timeframe, inst.kind), None)
        return tuple(sorted(seen, key=lambda s: order[s.strategy]))  # stable: config order

    def paused_slots(self) -> frozenset[Slot]:
        """The slots configured with `enabled: false` (US-067 D9), as (name, timeframe, market)."""
        from ..paper.ledger import Slot

        return frozenset(
            Slot(s.name, s.timeframe, inst.kind)
            for inst in self.instruments
            for s in inst.strategies
            if not s.enabled
        )

    def delivery_slots(self) -> tuple[tuple[DeskInstrumentCfg, StrategySlotCfg], ...]:
        """(stock, slot) for every enabled delivery slot (US-067), in configuration order."""
        return tuple(
            (inst, s)
            for inst in self.stocks
            for s in inst.strategies
            if s.product == "delivery" and s.enabled
        )

    @property
    def instruments(self) -> tuple[DeskInstrumentCfg, ...]:
        return self.indices + self.stocks


def _slots(raw: list | None) -> tuple[StrategySlotCfg, ...]:
    out = []
    for r in raw or []:
        if isinstance(r, str):
            out.append(StrategySlotCfg(r))
        else:
            out.append(
                StrategySlotCfg(
                    str(r["name"]),
                    str(r.get("timeframe", "5m")),
                    dict(r.get("params") or {}),
                    _flag(r.get("enabled", True), "enabled"),
                    _product(r.get("product", "intraday")),
                )
            )
    return tuple(out)


def _flag(value: object, name: str) -> bool:
    """A yes/no key: true or false only (a quoted "false" would read as true otherwise)."""
    if not isinstance(value, bool):
        raise ValueError(f"desk.yaml slot key {name} must be true or false, not {value!r}")
    return value


def _product(value: object) -> str:
    if value not in PRODUCTS:
        raise ValueError(f"desk.yaml slot product must be one of {PRODUCTS}, not {value!r}")
    return str(value)


#: a slot's product: the tracker's intraday strategies, or the delivery pullback rule
PRODUCTS = ("intraday", "delivery")


def load_desk_config(path: str | Path | None = None) -> DeskConfig:
    p = Path(path) if path else PROJECT_ROOT / "config" / "desk.yaml"
    raw = {}
    if p.exists():
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    defaults = raw.get("defaults") or {}
    idx_default = _slots(defaults.get("index_strategies")) or (StrategySlotCfg("ema_cross", "12m"),)
    stk_default = _slots(defaults.get("stock_strategies")) or (StrategySlotCfg("ema_cross", "15m"),)
    indices = tuple(
        DeskInstrumentCfg(
            str(e["key"]).upper(),
            str(e.get("tracked") or f"{e['key']}_OPT").upper(),
            "INDEX",
            _slots(e.get("strategies")) or idx_default,
            str(e.get("note") or ""),
        )
        for e in raw.get("indices") or []
    )
    stocks = tuple(
        DeskInstrumentCfg(
            str(e["key"]).upper(),
            str(e.get("tracked") or e["key"]).upper(),
            "STOCK",
            _slots(e.get("strategies")) or stk_default,
            str(e.get("note") or ""),
        )
        for e in raw.get("stocks") or []
    )
    context = tuple(
        ContextCfg(
            str(c["key"]).upper(),
            str(c.get("label") or c["key"]),
            bool(c.get("manual")),
            str(c.get("note") or ""),
        )
        for c in raw.get("context") or []
    )
    return DeskConfig(
        int(raw.get("poll_seconds", 60)),
        int(raw.get("history_days", 15)),
        str(raw.get("source", "auto")),
        int(raw.get("signal_valid_minutes", 10)),
        str(raw.get("stock_horizon", "swing")),
        int(raw.get("swing_max_sessions", 5)),
        int(raw.get("audit_days", 7)),
        context,
        indices,
        stocks,
        str(p),
        tuple(dict.fromkeys(str(n) for n in raw.get("primary_strategies") or ())),
        _expiry_mode(raw.get("option_expiry", "monthly")),
        int(raw.get("option_roll_days", 3)),
    )


def _expiry_mode(value: object) -> str:
    """monthly (also when the key is empty) or nearest; anything else is a config error."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "monthly"
    mode = str(value).strip().lower()
    if mode not in ("monthly", "nearest"):
        raise ValueError(f"desk.yaml option_expiry must be monthly or nearest, not {value!r}")
    return mode
