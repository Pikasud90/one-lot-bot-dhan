"""Contract selection from the cached option chain.

Fixes three v1 defects at once:

  A8 — `0.60 <= delta <= 0.70` matches ZERO puts, because put deltas are
       negative. Selection compares ABS(delta) and the tests assert a PE
       contract actually passes.
  B1 — a flat `spread <= Rs 1.50` cap rejects essentially every ITM monthly
       strike, whose spreads run Rs 3-15. Replaced by a relative sanity gate.
  A9 — greeks are never trusted blindly; rows whose broker greeks failed the
       local cross-check are excluded before they can be sized against.

Every rejected contract is recorded with its reason, so an empty result is
explainable rather than mysterious.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..broker.base import ChainRow
from ..config import ContractCfg
from ..costs import Quote


@dataclass
class CandidateRejection:
    strike: float
    opt_type: str
    reason: str

    def as_dict(self) -> dict:
        return {"strike": self.strike, "opt_type": self.opt_type, "reason": self.reason}


@dataclass
class SelectionResult:
    chosen: ChainRow | None
    quote: Quote | None
    rejections: list[CandidateRejection] = field(default_factory=list)
    considered: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.chosen is not None

    def rejection_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rejections:
            key = r.reason.split("(")[0].strip()
            out[key] = out.get(key, 0) + 1
        return out

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "considered": self.considered,
            "reason": self.reason,
            "chosen": (
                {
                    "strike": self.chosen.strike,
                    "opt_type": self.chosen.opt_type,
                    "security_id": self.chosen.security_id,
                    "bid": self.chosen.bid,
                    "ask": self.chosen.ask,
                    "delta": self.chosen.delta,
                    "iv": self.chosen.iv,
                    "oi": self.chosen.oi,
                    "volume": self.chosen.volume,
                    "greek_source": self.chosen.greek_source,
                }
                if self.chosen
                else None
            ),
            "rejection_summary": self.rejection_summary(),
        }


def select_contract(
    rows: list[ChainRow],
    direction: str,
    cfg: ContractCfg,
    lot_size: int = 35,
    target_delta: float | None = None,
) -> SelectionResult:
    """Pick the best contract for `direction` ('CE' or 'PE').

    Among everything that passes every filter, we choose the contract whose
    ABS(delta) is closest to the middle of the configured band — the most
    "typical" instrument rather than one clinging to a boundary, which makes
    behaviour stable as the underlying drifts.
    """
    result = SelectionResult(chosen=None, quote=None)
    mid_target = (
        target_delta if target_delta is not None else ((cfg.delta_min + cfg.delta_max) / 2.0)
    )

    side = [r for r in rows if r.opt_type == direction]
    result.considered = len(side)
    if not side:
        result.reason = f"no {direction} rows in the chain snapshot"
        return result

    survivors: list[tuple[float, ChainRow, Quote]] = []

    for r in side:
        # 1. greek trustworthiness (defect A9)
        if r.greek_source in ("rejected", "no_market", "unsolvable", "absent"):
            result.rejections.append(
                CandidateRejection(r.strike, r.opt_type, f"greeks unusable ({r.greek_source})")
            )
            continue
        if r.delta is None:
            result.rejections.append(
                CandidateRejection(r.strike, r.opt_type, "greeks unusable (delta missing)")
            )
            continue

        # 2. two-sided market
        q = Quote(bid=r.bid or 0.0, ask=r.ask or 0.0, ltp=r.ltp or 0.0)
        if not q.is_valid:
            result.rejections.append(
                CandidateRejection(
                    r.strike, r.opt_type, "no two-sided market (zero or crossed bid/ask)"
                )
            )
            continue

        # 3. delta band — ABS, because put deltas are negative (defect A8)
        adelta = abs(r.delta)
        if not (cfg.delta_min <= adelta <= cfg.delta_max):
            result.rejections.append(
                CandidateRejection(
                    r.strike,
                    r.opt_type,
                    f"delta outside band (|{r.delta:+.3f}| vs {cfg.delta_min:g}-{cfg.delta_max:g})",
                )
            )
            continue

        # 4. liquidity sanity — RELATIVE, not a flat rupee cap (defect B1)
        allowed = cfg.max_allowed_spread(q.mid)
        if q.spread > allowed:
            result.rejections.append(
                CandidateRejection(
                    r.strike,
                    r.opt_type,
                    f"spread too wide (Rs {q.spread:.2f} > Rs {allowed:.2f} allowed "
                    f"at mid Rs {q.mid:.2f})",
                )
            )
            continue

        # 5. depth — a tight spread on 1 lot is not liquidity
        min_qty = cfg.min_bid_qty_lots * lot_size
        if (r.bid_qty is not None and r.bid_qty < min_qty) or (
            r.ask_qty is not None and r.ask_qty < min_qty
        ):
            result.rejections.append(
                CandidateRejection(
                    r.strike,
                    r.opt_type,
                    f"insufficient depth (bid {r.bid_qty}, ask {r.ask_qty} < {min_qty})",
                )
            )
            continue

        # 6. open interest and traded volume
        if r.oi is not None and r.oi < cfg.min_open_interest:
            result.rejections.append(
                CandidateRejection(
                    r.strike,
                    r.opt_type,
                    f"open interest too low ({r.oi:,.0f} < {cfg.min_open_interest:,})",
                )
            )
            continue
        if r.volume is not None and r.volume < cfg.min_volume:
            result.rejections.append(
                CandidateRejection(
                    r.strike, r.opt_type, f"volume too low ({r.volume:,.0f} < {cfg.min_volume:,})"
                )
            )
            continue

        survivors.append((abs(adelta - mid_target), r, q))

    if not survivors:
        summary = result.rejection_summary()
        result.reason = "no contract passed selection: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(summary.items(), key=lambda kv: -kv[1])
        )
        return result

    survivors.sort(key=lambda t: t[0])
    _, best, quote = survivors[0]
    result.chosen, result.quote = best, quote
    result.reason = (
        f"selected {best.opt_type} {best.strike:g} "
        f"(|delta| {abs(best.delta):.3f}, spread Rs {quote.spread:.2f})"
    )
    return result
