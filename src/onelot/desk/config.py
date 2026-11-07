"""config/desk.yaml -> typed desk configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..settings import PROJECT_ROOT


@dataclass(frozen=True)
class StrategySlotCfg:
    name: str
    timeframe: str = "5m"
    params: dict = field(default_factory=dict)


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

    def primary_strategies(self) -> tuple[str, ...]:
        """The owner's primary strategies: desk.yaml `primary_strategies` in the owner's
        order, else every strategy the desk runs, in configuration order, each once. The
        Home scoreboard and the Lab's 'primary strategies' filter show them."""
        seen: dict[str, None] = dict.fromkeys(self.primary)
        for inst in self.instruments:
            for slot in inst.strategies:
                seen.setdefault(slot.name, None)
        return tuple(seen)

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
                    str(r["name"]), str(r.get("timeframe", "5m")), dict(r.get("params") or {})
                )
            )
    return tuple(out)


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
    )
