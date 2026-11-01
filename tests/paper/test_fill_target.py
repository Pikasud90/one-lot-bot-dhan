"""A fill bar that opens AT or BEYOND the alert target is NO_FILL (US-002, Manager decision 3).

Symmetric with gap_through_stop: the owner was told the alert's underlying levels, and a fill
past the target would book a 'target' on the wrong side of the entry. A long's open at or
above T, a short's at or below T; one tick short of T still fills. Judged on the underlying
for an index option (a BUY_PE's target is below).
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd
import pytest

from onelot.paper.fill import decide_fill, normalise_bars
from onelot.store.schema import paper_events, paper_trades

from .conftest import THU, ist, make_alert, make_bars, table_rows

CREATED = ist(THU, 10, 5, 3)
DEADLINE = ist(THU, 10, 15, 3)


def decide(direction: str, fill_open: float, target: int | None):
    stop = 99_500 if direction == "long" else 100_500
    bars = normalise_bars(make_bars(ist(THU, 10, 5), [1000.0, fill_open]))
    assert bars is not None
    return decide_fill(
        bars,
        created_at=CREATED,
        deadline=DEADLINE,
        resolution_min=1,
        now=ist(THU, 10, 7),
        direction=direction,
        stop_paise=stop,
        target_paise=target,
    )


class TestDecideFill:
    @pytest.mark.parametrize(
        ("direction", "fill_open", "target", "outcome", "reason"),
        [
            ("long", 1010.00, 101_000, "no_fill", "gap_through_target"),  # AT the target
            ("long", 1012.00, 101_000, "no_fill", "gap_through_target"),
            ("long", 1009.95, 101_000, "fill", None),  # one tick short
            ("short", 990.00, 99_000, "no_fill", "gap_through_target"),
            ("short", 988.00, 99_000, "no_fill", "gap_through_target"),
            ("short", 990.05, 99_000, "fill", None),
            ("long", 1012.00, None, "fill", None),  # no target, no target gap
        ],
    )
    def test_the_target_gap(self, direction, fill_open, target, outcome, reason) -> None:
        decision = decide(direction, fill_open, target)
        assert (decision.outcome, decision.reason) == (outcome, reason)
        assert decision.bar_ts == ist(THU, 10, 6)

    def test_the_target_is_optional_for_existing_callers(self) -> None:
        bars = normalise_bars(make_bars(ist(THU, 10, 5), [1000.0, 1012.0]))
        assert bars is not None
        decision = decide_fill(
            bars,
            created_at=CREATED,
            deadline=DEADLINE,
            resolution_min=1,
            now=ist(THU, 10, 7),
            direction="long",
            stop_paise=99_500,
        )
        assert decision.outcome == "fill"


def series(**frames: pd.DataFrame) -> Callable[[str], pd.DataFrame | None]:
    return lambda key: frames.get(key)


class TestThroughTheBook:
    @pytest.mark.parametrize(
        ("kind", "direction", "gap_open", "key"),
        [
            ("STOCK", "long", 1010.00, "RELIANCE"),  # target 1010.00
            ("STOCK", "short", 990.00, "RELIANCE"),  # target 990.00
            ("INDEX", "short", 45010.35, "BANKNIFTY"),  # BUY_PE: underlying target 45010.35
        ],
    )
    def test_no_fill_gap_through_target(self, make_book, engine, kind, direction, gap_open, key):
        book = make_book()
        book.on_alert(make_alert(kind, direction), ist(THU, 10, 5, 4))
        bars = make_bars(ist(THU, 10, 5), [gap_open, gap_open])

        events = book.step(ist(THU, 10, 8), series(**{key: bars}))

        (row,) = table_rows(engine, paper_trades)
        assert (row["status"], row["close_reason"]) == ("NO_FILL", "GAP_THROUGH_TARGET")
        assert row["entry_paise"] is None and row["exit_paise"] is None
        assert [(e.kind, e.detail["reason"], e.ts) for e in events] == [
            ("NO_FILL", "gap_through_target", ist(THU, 10, 6))
        ]
        assert [e["kind"] for e in table_rows(engine, paper_events)] == ["OPENED", "NO_FILL"]
        assert book.open_trades() == []
