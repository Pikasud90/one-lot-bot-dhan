"""US-100: the place counting of the capped pullback book (`scripts/pullback_book.py`) on
hand-made trades. No research file is read: the book's function is pure."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "pullback_book", ROOT / "scripts" / "pullback_book.py"
)
pb = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(pb)  # type: ignore[union-attr]


def run(trades, k, money=1e9, n=10, cash=True):
    """trades: (signal, entry, exit, amount, net, rank) per trade."""
    a = np.array(trades, dtype=float)
    return pb.run_book(
        a[:, 0].astype(int), a[:, 1].astype(int), a[:, 2].astype(int), a[:, 3], a[:, 4],
        a[:, 5], k, money, n, cash,
    )  # fmt: skip


def test_same_evening_signals_take_free_places_in_rank_order():
    st, used = run([(0, 1, 5, 100, 0, 2), (0, 1, 5, 100, 0, 0), (0, 1, 5, 100, 0, 1)], k=2)
    assert list(st) == [pb.CAPPED, pb.DONE, pb.DONE]
    assert used[0] == 2 and used[4] == 2 and used[5] == 0


def test_an_exit_at_the_open_frees_the_place_that_evening():
    # the first trade exits at the open of session 3: a signal on evening 3 gets its place
    st, used = run([(0, 1, 3, 100, 0, 0), (3, 4, 6, 100, 0, 0)], k=1)
    assert list(st) == [pb.DONE, pb.DONE]
    assert used[2] == 1 and used[3] == 1


def test_a_trade_waiting_to_exit_still_holds_its_place():
    # exit signalled on the close of 3, filled at the open of 4: on evening 3 the place is held
    st, _ = run([(0, 1, 4, 100, 0, 0), (3, 4, 6, 100, 0, 0)], k=1)
    assert list(st) == [pb.DONE, pb.CAPPED]


def test_a_pending_entry_holds_a_place_and_a_place_is_kept_until_closed():
    # signal on evening 0, entry only at session 2 (no bar on 1): evening 1 has no free place
    st, _ = run([(0, 2, 8, 100, 0, 0), (1, 2, 8, 100, 0, 0)], k=1)
    assert list(st) == [pb.DONE, pb.CAPPED]


def test_open_at_the_end_holds_its_place():
    st, used = run([(0, 1, 10, 100, 0, 0), (6, 7, 9, 100, 0, 0)], k=1)
    assert list(st) == [pb.OPEN, pb.CAPPED]
    assert used[-1] == 1


def test_cash_refusal_at_the_fill_loses_the_place_for_that_evening_only():
    # money 150: the first trade (100) is paid, the second (100) is refused at its fill;
    # it held a place on evening 0, so the third signal of evening 0 was capped
    st, used = run(
        [(0, 1, 5, 100, 0, 0), (0, 1, 5, 100, 0, 1), (0, 1, 5, 100, 0, 2), (2, 3, 5, 40, 0, 0)],
        k=2,
        money=150,
    )
    assert list(st) == [pb.DONE, pb.REFUSED, pb.CAPPED, pb.DONE]
    assert used[0] == 2 and used[1] == 1 and used[2] == 2


def test_an_exit_pays_back_before_the_same_morning_entries():
    # the first trade sells at the open of 3 for 100 + 20; the second fills that morning
    st, _ = run([(0, 1, 3, 100, 20, 0), (2, 3, 6, 110, 0, 0)], k=2, money=100)
    assert list(st) == [pb.DONE, pb.DONE]


def test_without_the_cash_rule_nothing_is_refused():
    st, _ = run([(0, 1, 5, 100, 0, 0), (0, 1, 5, 100, 0, 1)], k=5, money=0, cash=False)
    assert list(st) == [pb.DONE, pb.DONE]
