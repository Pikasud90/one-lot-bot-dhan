"""config/desk.yaml -> typed desk configuration."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..clock import parse_hhmm
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
    # US-078: false = a training instrument: tracked and shown, but it raises no alert
    trade: bool = True


@dataclass(frozen=True)
class DeliveryCfg:
    """desk.yaml `delivery:` (US-067): how the delivery slots read their days and top them up."""

    top_up: bool = True  # top the store's one-minute bars up from Dhan outside market hours
    last_bar_at: str = "15:10"  # a session is complete once its last fine bar is at/after this
    settle_minutes: int = 10  # ... and the clock is this long past the session's close
    min_bars: int = 201  # daily bars an entry reads at least (D8; the rule's own 201)
    gap_warn_pct: float = 20.0  # D15: a first bar this far from the previous close is flagged
    special_min_bars: int = 30  # a non-calendar day is a special session from this many 1m bars


@dataclass(frozen=True)
class VolumeCfg:
    """desk.yaml `volume:` (US-089, `.agile/stories/VOL-API.md`): the Volume Watch. The levels
    come from docs/research/VOLUME_STUDY_2026-11-24.md."""

    basket: tuple[str, ...] = ()  # empty = the desk's stocks
    rule: str = "bvc"  # the up-share rule: clv | tick | bvc (the study chose bvc)
    z_alert: float = 2.5  # a market alert from this spike score
    stock_z_alert: float = 4.0  # a single-stock alert from this spike score
    cooldown_min: int = 15  # no second alert of the same scope and key inside this
    alerts_on: bool = True  # false: the watch decides no alert at all
    notify: bool = True  # false: alerts are stored and shown, never sent
    book: bool = True  # one quote request a minute for the basket's order book
    max_alerts_per_day: int = 12
    quiet: float = 1.0  # rvol_5m below this: quiet (the study's 15th percentile, 0.95)
    busy: float = 2.1  # rvol_5m at or above this: busy (the study's 85th percentile)


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
    delivery: DeliveryCfg = field(default_factory=DeliveryCfg)
    volume: VolumeCfg = field(default_factory=VolumeCfg)

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


def _trade_flag(value: object, key: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"desk.yaml instrument {key}: trade must be true or false, not {value!r}")
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
            _trade_flag(e.get("trade", True), e["key"]),
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
        _delivery(raw.get("delivery")),
        _volume(raw.get("volume")),
    )


def _delivery(raw: object) -> DeliveryCfg:
    """desk.yaml `delivery:`; an unknown key is an error, never silently ignored."""
    if raw is None:
        return DeliveryCfg()
    if not isinstance(raw, dict):
        raise ValueError(f"desk.yaml delivery must be a mapping, not {raw!r}")
    known = {f.name for f in fields(DeliveryCfg)}
    extra = sorted(set(raw) - known)
    if extra:
        raise ValueError(f"desk.yaml delivery: unknown key(s) {extra}")
    cfg = DeliveryCfg(
        top_up=_flag(raw.get("top_up", True), "delivery.top_up"),
        last_bar_at=str(raw.get("last_bar_at", "15:10")),
        settle_minutes=int(raw.get("settle_minutes", 10)),
        min_bars=int(raw.get("min_bars", 201)),
        gap_warn_pct=float(raw.get("gap_warn_pct", 20.0)),
        special_min_bars=int(raw.get("special_min_bars", 30)),
    )
    parse_hhmm(cfg.last_bar_at)  # a bad time is a config error now, not at the close
    return cfg


def _volume(raw: object) -> VolumeCfg:
    """desk.yaml `volume:`; an unknown key or a bad value is an error, never silently ignored."""
    if raw is None:
        return VolumeCfg()
    if not isinstance(raw, dict):
        raise ValueError(f"desk.yaml volume must be a mapping, not {raw!r}")
    known = {f.name for f in fields(VolumeCfg)}
    extra = sorted(set(raw) - known)
    if extra:
        raise ValueError(f"desk.yaml volume: unknown key(s) {extra}")
    d = VolumeCfg()
    basket = raw.get("basket") or ()
    if isinstance(basket, str) or not isinstance(basket, (list, tuple)):
        raise ValueError(f"desk.yaml volume.basket must be a list of stock keys, not {basket!r}")
    rule = str(raw.get("rule", d.rule))
    if rule not in ("clv", "tick", "bvc"):
        raise ValueError(f"desk.yaml volume.rule must be clv, tick or bvc, not {rule!r}")
    cfg = VolumeCfg(
        basket=tuple(dict.fromkeys(str(k).upper() for k in basket)),
        rule=rule,
        z_alert=float(raw.get("z_alert", d.z_alert)),
        stock_z_alert=float(raw.get("stock_z_alert", d.stock_z_alert)),
        cooldown_min=int(raw.get("cooldown_min", d.cooldown_min)),
        alerts_on=_flag(raw.get("alerts_on", d.alerts_on), "volume.alerts_on"),
        notify=_flag(raw.get("notify", d.notify), "volume.notify"),
        book=_flag(raw.get("book", d.book), "volume.book"),
        max_alerts_per_day=int(raw.get("max_alerts_per_day", d.max_alerts_per_day)),
        quiet=float(raw.get("quiet", d.quiet)),
        busy=float(raw.get("busy", d.busy)),
    )
    if cfg.cooldown_min < 0 or cfg.max_alerts_per_day < 0:
        raise ValueError("desk.yaml volume: cooldown_min and max_alerts_per_day must be >= 0")
    return cfg


def _expiry_mode(value: object) -> str:
    """monthly (also when the key is empty) or nearest; anything else is a config error."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "monthly"
    mode = str(value).strip().lower()
    if mode not in ("monthly", "nearest"):
        raise ValueError(f"desk.yaml option_expiry must be monthly or nearest, not {value!r}")
    return mode
