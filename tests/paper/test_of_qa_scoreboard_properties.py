"""Tester (QA) properties of the Home strategy scoreboard (owner feedback OF-5).

Whatever the paper book holds, the scoreboard must agree with the ledger it is cut from:

* summed over strategies it equals `PaperLedger.totals()` to the paisa (the board never invents or
  loses money);
* a strategy's wins and losses never exceed its trades, and its winning and losing money add up to
  its net;
* the displayed ratios follow from the exact sums (win rate, average, profit factor);
* the owner's primary strategies always lead, in the owner's order, with or without trades.
"""

from __future__ import annotations

import tempfile
from decimal import ROUND_HALF_EVEN, Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from onelot.paper.ledger import PaperLedger
from onelot.paper.money import format_rupees
from onelot.paper.view import scoreboard_view
from onelot.store import init_database

from .test_scoreboard import insert

NAMES = ["ema_cross", "supertrend_vol", "gap_fade", "nr7_breakout", "turtle_soup", "legacy_orb"]

trade = st.tuples(
    st.sampled_from(NAMES),
    st.integers(min_value=-500_000, max_value=500_000),  # net, paise
    st.integers(min_value=0, max_value=20_000),  # costs, paise
    st.sampled_from(["CLOSED", "CLOSED", "CLOSED", "NO_FILL"]),
    st.one_of(st.none(), st.integers(min_value=0, max_value=100_000)),  # risk, paise
)


def build(trades):
    folder = tempfile.TemporaryDirectory()
    db = init_database(f"sqlite:///{folder.name}/board.db")
    for i, (name, net, costs, status, risk) in enumerate(trades):
        insert(
            db.engine,
            f"a{i}",
            name,
            None if status == "NO_FILL" else net,
            status=status,
            costs=costs,
            risk=risk,
        )
    return folder, db


@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(st.lists(trade, max_size=25), st.permutations(NAMES[:5]))
def test_the_board_agrees_with_the_ledger_and_with_itself(trades, primary):
    folder, db = build(trades)
    try:
        ledger = PaperLedger(db.engine)
        per = ledger.by_strategy()
        whole = ledger.totals()
        assert sum(r.trades for r in per.values()) == whole.trades
        assert sum(r.wins for r in per.values()) == whole.wins
        assert sum(r.losses for r in per.values()) == whole.losses
        assert sum(r.gross_paise for r in per.values()) == whole.gross_paise
        assert sum(r.costs_paise for r in per.values()) == whole.costs_paise
        assert sum(r.net_paise for r in per.values()) == whole.net_paise
        assert sum(r.no_fill for r in per.values()) == sum(1 for t in trades if t[3] == "NO_FILL")
        for r in per.values():
            assert r.wins + r.losses <= r.trades
            assert r.won_paise + r.lost_paise == r.net_paise
            assert r.won_paise >= 0 >= r.lost_paise
            assert r.gross_paise - r.costs_paise == r.net_paise

        rows = scoreboard_view(per, primary)
        assert [r["strategy"] for r in rows][: len(primary)] == list(primary)
        assert all(r["primary"] for r in rows[: len(primary)])
        assert len({r["strategy"] for r in rows}) == len(rows)
        for r in rows:
            t = per.get(r["strategy"])
            if t is None or t.trades == 0:
                assert r["win_rate"] is None and r["net"] is None and r["avg_net"] is None
                continue
            assert r["win_rate"] == round(100.0 * t.wins / t.trades, 1)
            assert r["net"] == format_rupees(t.net_paise)
            avg = int(
                (Decimal(t.net_paise) / Decimal(t.trades)).quantize(
                    Decimal(1), rounding=ROUND_HALF_EVEN
                )
            )
            assert r["avg_net"] == format_rupees(avg)
            if t.lost_paise < 0:
                assert r["profit_factor"] == round(t.won_paise / -t.lost_paise, 2)
            else:
                assert r["profit_factor"] is None
    finally:
        db.dispose()
        folder.cleanup()
