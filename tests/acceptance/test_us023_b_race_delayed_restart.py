"""US-023 mode B: the cost-cache race, grade-delayed trades, and a restart in the middle of a trade.

* `economics._costs_for` is called from the desk thread, API threads and the Lab's workers. On a
  COLD process, 16 threads released together while the one parse of costs.yaml is made slow
  (0.2 s) must still cause exactly ONE parse (a cache that checks and fills without a lock would
  parse once per thread) and every thread must get the table of its own product;
* a DELAYED (Yahoo) option trade is marked on DATA time, `now - paper.feed_delay_minutes`, never
  on the freshest bar the feed has; 15 minutes of wall time later it holds exactly the marks, and
  books exactly the money, of a live trade on the same bars; nothing is marked, and nothing is
  called a failure, while no bar has closed by data time;
* a book that restarts between a trade's fill and its close keeps the mark, does not announce a
  failure streak a second time, and closes the trade to exactly the money an uninterrupted
  book books.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W

from onelot.desk import economics

STOCK, OPTION = H.SPECS["SBIN"], H.SPECS["BANKNIFTY_OPT"]

RACE_SCRIPT = """
import json, threading, time, yaml
from pathlib import Path
import onelot.config as config

counts = {"load": 0, "yaml": 0}
real_safe_load, real_load = yaml.safe_load, config.CostConfig.load.__func__

def counting_load(cls, path):
    if Path(str(path)).name == "costs.yaml":
        counts["load"] += 1
    return real_load(cls, path)

def slow_counting_safe_load(stream):
    result = real_safe_load(stream)
    if isinstance(result, dict) and {"brokerage", "taxes", "effective_from"} <= set(result):
        counts["yaml"] += 1
        time.sleep(0.2)  # a wide window: an unlocked check-then-fill parses once per thread in it
    return result

config.CostConfig.load = classmethod(counting_load)
yaml.safe_load = slow_counting_safe_load

from onelot.desk import economics
from onelot.data.universe import InstrumentSpec

stock = InstrumentSpec("SBIN", "SBIN", "STOCK", "SBIN", 1, 0.05, "equity_intraday", 0.0)
option = InstrumentSpec("BANKNIFTY_OPT", "BN", "OPTIONS", "BANKNIFTY", 30, 0.05, "index_options", 100.0)

N = 16
barrier = threading.Barrier(N)
seen = [None] * N
errors = []

def work(i):
    spec = stock if i % 2 == 0 else option
    try:
        barrier.wait()
        models = [economics._costs_for(spec) for _ in range(20)]
        seen[i] = sorted({(m.cfg.taxes.stt_sell_pct, m.cfg.brokerage.mode) for m in models})
    except BaseException as exc:
        errors.append(repr(exc))

threads = [threading.Thread(target=work, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)
print(json.dumps({"counts": counts, "seen": seen, "errors": errors}))
"""


def race_in_a_cold_process() -> dict:
    done = subprocess.run(
        [sys.executable, "-c", RACE_SCRIPT],
        cwd=W.ROOT,
        env={"PYTHONPATH": str(W.ROOT / "src"), "PATH": "/usr/bin:/bin", "HOME": str(W.ROOT)},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, f"cold race process failed:\n{done.stderr[-2000:]}"
    return dict(json.loads(done.stdout.strip().splitlines()[-1]))


class TestTheCostCacheUnderThreads:
    def test_sixteen_threads_on_a_cold_cache_parse_costs_yaml_exactly_once(self):
        result = race_in_a_cold_process()
        assert result["errors"] == []
        assert max(result["counts"].values()) == 1, result["counts"]

    def test_every_thread_gets_the_table_of_its_own_product(self):
        seen = race_in_a_cold_process()["seen"]
        assert len(seen) == 16
        assert seen[0::2] == [[[0.00025, "min_of"]]] * 8, "SBIN: equity_intraday"
        assert seen[1::2] == [[[0.0010, "flat"]]] * 8, "BANKNIFTY_OPT: index_options"

    def test_threads_on_a_warm_cache_never_parse_and_never_cross_products(self):
        economics._costs_for(STOCK)
        economics._costs_for(OPTION)
        wrong: list[str] = []

        def work(i: int) -> None:
            spec, stt = (STOCK, 0.00025) if i % 2 == 0 else (OPTION, 0.0010)
            for _ in range(200):
                if economics._costs_for(spec).cfg.taxes.stt_sell_pct != stt:
                    wrong.append(spec.key)

        with W.count_cost_table_parses() as counts:
            threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
        assert wrong == []
        assert max(counts.values()) == 0, counts


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


CE_ROWS = [
    W.CE_FILL,
    (5400500, 5405500, 5400000, 5405000),
    (5405000, 5409000, 5403000, 5408000),
    (5408000, 5410000, 5399500, 5400000),
]
NOWS = [H.at(10, 7), H.at(10, 8), H.at(10, 9), H.at(10, 10)]
SHIFT = 15  # paper.feed_delay_minutes of the harness book


def shifted_nows(minutes: int):
    return [t + dt.timedelta(minutes=minutes) for t in NOWS]


class TestADelayedTradeIsMarkedOnDataTime:
    def run_marks(self, engine, source, nows, alert_id="O1"):
        alert = U.make_alert(alert_id, kind="INDEX", direction="long", source=source)
        book = W.make_book(engine, premium_at=H.make_pricer())
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS])
        marks = []
        for now in nows:
            U.step_book(book, frame, now, series="BANKNIFTY")
            marks.append(W.mark_of(engine, alert_id))
        return alert, book, marks

    def test_marks_lag_the_wall_clock_by_the_feed_delay_and_never_use_the_freshest_bar(
        self, tmp_path
    ):
        d = H.new_db(tmp_path, "delayed.db")
        try:
            alert, book, marks = self.run_marks(
                d.engine, "yahoo", [H.at(10, 20), H.at(10, 23), H.at(10, 25)]
            )
            assert U.row_of(d.engine, "O1")["grade"] == "delayed"
            # 10:20 wall = 10:05 data: no bar has closed yet; 10:23 -> 10:08: the bar of 10:07
            # (close 54050) is the last closed; 10:25 -> 10:10: the bar of 10:09 (close 54000).
            assert marks == [None, W.fake_mark(alert, 5405000), W.fake_mark(alert, 5400000)]
            assert W.pricing_events(d.engine, "O1") == [], "no bar yet is not a failed repricing"
            assert book.status()["hook_errors"] == 0
        finally:
            d.dispose()

    def test_fifteen_minutes_later_a_delayed_trade_holds_the_marks_of_a_live_one(self, tmp_path):
        live_db, slow_db = H.new_db(tmp_path, "live.db"), H.new_db(tmp_path, "slow.db")
        try:
            _, _, live = self.run_marks(live_db.engine, "dhan", NOWS)
            _, _, slow = self.run_marks(slow_db.engine, "yahoo", shifted_nows(SHIFT))
            assert live == slow and all(isinstance(m, int) for m in live), (live, slow)
            assert len(set(live)) == 4
        finally:
            live_db.dispose()
            slow_db.dispose()

    @pytest.mark.parametrize("name", list(W.OPTION_SCENARIOS))
    def test_a_delayed_option_trade_books_the_money_of_its_live_twin(self, tmp_path, name):
        direction, rows, _, _ = W.OPTION_SCENARIOS[name]
        outcomes = {}
        for source, shift in (("dhan", 0), ("yahoo", SHIFT)):
            d = H.new_db(tmp_path, f"{source}.db")
            try:
                alert = U.make_alert("O1", kind="INDEX", direction=direction, source=source)
                end = H.at(10, 9) + dt.timedelta(minutes=shift)
                _, row, _ = W.run_option(d.engine, rows, alert=alert, now=end)
                assert row["status"] == "CLOSED" and row["costs_paise"] is not None
                outcomes[source] = {
                    c: row[c]
                    for c in (*W.PAISE_COLUMNS, "close_reason", "exit_ts", "qty", "product")
                }
                outcomes[source]["grade"] = row["grade"]
            finally:
                d.dispose()
        assert outcomes["dhan"].pop("grade") == "live"
        assert outcomes["yahoo"].pop("grade") == "delayed"
        assert outcomes["dhan"] == outcomes["yahoo"]

    def test_a_delayed_stock_trade_books_the_money_of_its_live_twin_and_the_ledger_splits_grades(
        self, tmp_path
    ):
        d = H.new_db(tmp_path, "grades.db")
        try:
            book = W.make_book(d.engine)
            for alert_id, key, source in (("LIVE", "SBIN", "dhan"), ("SLOW", "RELIANCE", "yahoo")):
                alert = W.stock_alert(alert_id, key=key, source=source)
                H.open_trade(book, alert)
            frames = {
                key: U.pbars(
                    H.at(10, 5),
                    [U.PRE_BAR, *W.stock_rows(W.stock_alert("X", key=key), "target")],
                )
                for key in ("SBIN", "RELIANCE")
            }
            book.step(H.at(10, 9) + dt.timedelta(minutes=SHIFT), H.Feed(**frames))
            live, slow = U.row_of(d.engine, "LIVE"), U.row_of(d.engine, "SLOW")
            assert (live["grade"], slow["grade"]) == ("live", "delayed")
            for column in ("entry_paise", "exit_paise", "gross_paise", "costs_paise", "net_paise"):
                assert live[column] == slow[column] and live[column] is not None, column
            ledger = W.make_ledger(d.engine)
            assert ledger.totals(grade="live").trades == 1
            assert ledger.totals(grade="delayed").trades == 1
            assert ledger.totals().net_paise == 2 * 1863
        finally:
            d.dispose()


class TestARestartInTheMiddleOfATrade:
    def uninterrupted(self, engine, rows, direction):
        alert = U.make_alert("O1", kind="INDEX", direction=direction)
        _, row, _ = W.run_option(engine, rows, alert=alert)
        return row

    @pytest.mark.parametrize("name", ["ce_target", "ce_stop", "pe_target", "pe_stop"])
    def test_a_restart_between_fill_and_close_books_the_same_money(self, tmp_path, name):
        direction, rows, _, _ = W.OPTION_SCENARIOS[name]
        whole, split = H.new_db(tmp_path, "whole.db"), H.new_db(tmp_path, "split.db")
        try:
            want = self.uninterrupted(whole.engine, rows, direction)
            alert = U.make_alert("O1", kind="INDEX", direction=direction)
            first = W.make_book(split.engine, premium_at=H.make_pricer())
            H.open_trade(first, alert)
            frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *rows])
            U.step_book(first, frame, H.at(10, 7), series="BANKNIFTY")  # only the fill bar
            assert U.row_of(split.engine, "O1")["status"] == "OPEN"
            mark = W.mark_of(split.engine)
            assert isinstance(mark, int)
            reborn = W.make_book(split.engine, premium_at=H.make_pricer())  # the restart
            assert [t.alert_id for t in reborn.open_trades()] == ["O1"]
            assert W.mark_of(split.engine) == mark, "the mark survives a restart"
            U.step_book(reborn, frame, H.at(10, 9), series="BANKNIFTY")
            got = U.row_of(split.engine, "O1")
            for column in (*W.PAISE_COLUMNS, "status", "close_reason", "exit_ts"):
                assert got[column] == want[column], column
            assert got["costs_paise"] is not None
        finally:
            whole.dispose()
            split.dispose()

    def test_a_restart_inside_a_failure_streak_does_not_announce_it_again(self, tmp_path):
        d = H.new_db(tmp_path, "streak.db")
        try:
            pricer = W.SwitchablePricer(None)
            alert = U.make_alert("O1", kind="INDEX", direction="long")
            book = W.make_book(d.engine, premium_at=pricer)
            H.open_trade(book, alert)
            frame = U.pbars(H.at(10, 5), [W.OPT_PRE, *CE_ROWS])
            U.step_book(book, frame, NOWS[0], series="BANKNIFTY")
            pricer.failing = True
            U.step_book(book, frame, NOWS[1], series="BANKNIFTY")
            assert len(W.pricing_events(d.engine, "O1", leg="mark")) == 1
            still_down = W.SwitchablePricer(None)
            still_down.failing = True
            reborn = W.make_book(d.engine, premium_at=still_down)
            U.step_book(reborn, frame, NOWS[2], series="BANKNIFTY")
            U.step_book(reborn, frame, NOWS[3], series="BANKNIFTY")
            assert W.position_of(d.engine).get("mark_paise") is None
            assert len(W.pricing_events(d.engine, "O1", leg="mark")) == 1, "one streak, one event"
        finally:
            d.dispose()

    def test_a_stock_closed_after_a_restart_books_the_same_money(self, tmp_path):
        whole, split = H.new_db(tmp_path, "w.db"), H.new_db(tmp_path, "s.db")
        try:
            _, want, _ = W.run_stock(whole.engine, direction="short", key="MRF", entry=100000.0)
            alert = W.stock_alert("A1", direction="short", key="MRF", entry=100000.0)
            first = W.make_book(split.engine)
            H.open_trade(first, alert)
            frame = U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(alert, "target")])
            U.step_book(first, frame, H.at(10, 7), series="MRF")
            assert U.row_of(split.engine, "A1")["status"] == "OPEN"
            reborn = W.make_book(split.engine)
            U.step_book(reborn, frame, H.at(10, 9), series="MRF")
            got = U.row_of(split.engine, "A1")
            for column in (*W.PAISE_COLUMNS, "status", "close_reason", "exit_ts"):
                assert got[column] == want[column], column
            assert (got["costs_paise"], got["net_paise"]) == (8254, 197000 - 8254)
        finally:
            whole.dispose()
            split.dispose()
