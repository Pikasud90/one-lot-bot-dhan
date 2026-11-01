"""Storage layer: timezone integrity, idempotency, crash-recovery queries."""

from __future__ import annotations

import datetime as dt

import pytest

from onelot.clock import IST, now_ist
from onelot.store import init_database
from onelot.store.repo import Repo


@pytest.fixture()
def repo(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'test.db'}")
    yield Repo(db)
    db.dispose()


@pytest.fixture()
def spaced_repo(tmp_path):
    """The real project path contains spaces (v1 defect E5)."""
    d = tmp_path / "Web Application 6 - Bank Nifty" / "data"
    d.mkdir(parents=True)
    db = init_database(f"sqlite:///{d / 'onelot.db'}")
    yield Repo(db)
    db.dispose()


class TestSchema:
    def test_all_tables_created(self, repo):
        names = repo.db.table_names()
        for expected in (
            "candles",
            "chain_snapshots",
            "signals",
            "positions",
            "trade_journal",
            "gate_audit",
            "risk_state",
            "atm_iv_history",
            "instruments",
            "regime_history",
        ):
            assert expected in names

    def test_works_from_a_path_containing_spaces(self, spaced_repo):
        ts = now_ist().replace(microsecond=0)
        spaced_repo.upsert_candle("X", 5, ts, 1, 2, 0.5, 1.5, None)
        assert len(spaced_repo.get_candles("X", 5)) == 1


class TestTimezone:
    """v1 defect C5. SQLite silently drops tzinfo; a naive read on a non-IST
    machine shifts every bar."""

    def test_ist_round_trips_exactly(self, repo):
        ts = dt.datetime(2026, 8, 20, 9, 20, tzinfo=IST)
        repo.upsert_candle("BANKNIFTY_IDX", 5, ts, 54000, 54100, 53900, 54050, None)
        got = repo.get_candles("BANKNIFTY_IDX", 5)[0]["ts"]
        assert got == ts
        assert got.tzinfo is not None
        assert got.astimezone(IST).hour == 9 and got.astimezone(IST).minute == 20

    def test_utc_input_is_normalised_not_mangled(self, repo):
        utc_ts = dt.datetime(2026, 8, 20, 3, 50, tzinfo=dt.timezone.utc)  # 09:20 IST
        repo.upsert_candle("Y", 5, utc_ts, 1, 2, 0.5, 1.5, None)
        got = repo.get_candles("Y", 5)[0]["ts"].astimezone(IST)
        assert (got.hour, got.minute) == (9, 20)


class TestCandles:
    def test_upsert_is_idempotent(self, repo):
        ts = now_ist().replace(microsecond=0)
        for close in (54050.0, 54090.0, 54120.0):
            repo.upsert_candle("K", 5, ts, 54000, 54200, 53900, close, None)
        rows = repo.get_candles("K", 5)
        assert len(rows) == 1 and rows[0]["close"] == 54120.0

    def test_returned_oldest_first_for_indicators(self, repo):
        base = dt.datetime(2026, 8, 20, 9, 15, tzinfo=IST)
        for i in range(10):
            repo.upsert_candle("K", 5, base + dt.timedelta(minutes=5 * i), 1, 2, 0.5, 1.0 + i, None)
        rows = repo.get_candles("K", 5)
        assert [r["close"] for r in rows] == [1.0 + i for i in range(10)]

    def test_forming_bars_are_excluded_by_default(self, repo):
        """INVARIANT I2 at the storage boundary."""
        base = dt.datetime(2026, 8, 20, 9, 15, tzinfo=IST)
        repo.upsert_candle("K", 5, base, 1, 2, 0.5, 1.0, None, closed=True)
        repo.upsert_candle(
            "K", 5, base + dt.timedelta(minutes=5), 1, 2, 0.5, 2.0, None, closed=False
        )
        assert len(repo.get_candles("K", 5)) == 1
        assert len(repo.get_candles("K", 5, closed_only=False)) == 2

    def test_index_volume_stays_null_not_zero(self, repo):
        """A stored 0 would let VWAP produce a number (defect B3)."""
        repo.upsert_candle("IDX", 5, now_ist(), 1, 2, 0.5, 1.5, None)
        assert repo.get_candles("IDX", 5)[0]["volume"] is None


class TestSignalIdempotency:
    """v1 defect D7: two UIs, two sources of truth, possible double trade."""

    def _sig(self, repo, sid="S1"):
        now = now_ist()
        repo.insert_signal(
            {
                "signal_id": sid,
                "eval_id": "E1",
                "created_at": now,
                "expires_at": now + dt.timedelta(seconds=75),
                "direction": "CE",
                "underlying": "BANKNIFTY",
                "expiry": "2026-09-29",
                "strike": 54000.0,
                "opt_type": "CE",
                "status": "PENDING",
            }
        )
        return now

    def test_first_ack_wins_second_is_a_noop(self, repo):
        self._sig(repo)
        assert repo.resolve_signal("S1", "TAKEN", "dashboard", now_ist()) is True
        assert repo.resolve_signal("S1", "TAKEN", "telegram", now_ist()) is False
        assert repo.get_signal("S1")["resolution_source"] == "dashboard"

    def test_conflicting_acks_cannot_both_apply(self, repo):
        self._sig(repo)
        assert repo.resolve_signal("S1", "TAKEN", "dashboard", now_ist())
        assert not repo.resolve_signal("S1", "SKIPPED", "dashboard", now_ist())
        assert repo.get_signal("S1")["status"] == "TAKEN"

    def test_ttl_expiry(self, repo):
        now = self._sig(repo)
        assert repo.expire_stale_signals(now + dt.timedelta(seconds=10)) == 0
        assert repo.expire_stale_signals(now + dt.timedelta(seconds=120)) == 1
        assert repo.get_signal("S1")["status"] == "EXPIRED"

    def test_expired_signal_cannot_be_acked(self, repo):
        now = self._sig(repo)
        repo.expire_stale_signals(now + dt.timedelta(seconds=120))
        assert not repo.resolve_signal("S1", "TAKEN", "dashboard", now_ist())


class TestCrashRecovery:
    """v1 defect D5: nothing was persisted, so a restart lost the position."""

    def test_open_positions_survive_a_reconnect(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'recover.db'}"
        db = init_database(url)
        Repo(db).insert_position(
            {
                "position_id": "P1",
                "signal_id": "S1",
                "state": "POSITION_OPEN",
                "opened_at": now_ist(),
                "direction": "CE",
                "underlying": "BANKNIFTY",
                "strike": 54000.0,
                "opt_type": "CE",
                "lots": 2,
                "qty": 70,
                "lot_size": 35,
                "entry_price": 310.0,
                "initial_stop": 285.0,
                "target": 375.0,
                "mode": "PAPER",
            }
        )
        db.dispose()

        db2 = init_database(url)  # simulate the engine restarting
        open_ = Repo(db2).open_positions()
        assert len(open_) == 1
        assert open_[0]["entry_price"] == 310.0
        assert open_[0]["initial_stop"] == 285.0
        db2.dispose()

    def test_closed_positions_are_not_resumed(self, repo):
        repo.insert_position(
            {
                "position_id": "P2",
                "state": "CLOSED",
                "opened_at": now_ist(),
                "direction": "CE",
                "entry_price": 1.0,
                "initial_stop": 0.5,
                "mode": "PAPER",
            }
        )
        assert repo.open_positions() == []


class TestGateAudit:
    def test_rejection_histogram_answers_why_no_signal(self, repo):
        now = now_ist()
        repo.record_gates(
            "E1",
            now,
            "CE",
            [
                {"gate": "regime", "passed": True, "value": "BULL", "reason": ""},
                {"gate": "ivp", "passed": False, "value": "62", "reason": "IVP too high"},
                {"gate": "contract", "passed": False, "value": "", "reason": "spread"},
            ],
        )
        repo.record_gates(
            "E2",
            now,
            "CE",
            [
                {"gate": "regime", "passed": True, "value": "BULL", "reason": ""},
                {"gate": "ivp", "passed": False, "value": "58", "reason": "IVP too high"},
                {"gate": "contract", "passed": True, "value": "", "reason": ""},
            ],
        )
        hist = repo.gate_rejection_histogram(now - dt.timedelta(minutes=1))
        top = hist[0]
        assert top["gate"] == "ivp" and top["rejections"] == 2
        assert next(h for h in hist if h["gate"] == "regime")["pass_rate"] == 1.0


class TestAtmIvHistory:
    def test_excludes_today_so_iv_is_not_ranked_against_itself(self, repo):
        for i, iv in enumerate([0.12, 0.13, 0.14]):
            repo.record_atm_iv(
                f"2026-08-{18 + i:02d}", "BANKNIFTY", iv, 54000.0, 54000.0, now_ist()
            )
        assert repo.atm_iv_series("BANKNIFTY") == [0.12, 0.13, 0.14]
        assert repo.atm_iv_series("BANKNIFTY", before_date="2026-08-20") == [0.12, 0.13]

    def test_same_day_rerecord_updates_rather_than_duplicating(self, repo):
        repo.record_atm_iv("2026-08-20", "BANKNIFTY", 0.12, 54000.0, 54000.0, now_ist())
        repo.record_atm_iv("2026-08-20", "BANKNIFTY", 0.15, 54000.0, 54000.0, now_ist())
        assert repo.atm_iv_series("BANKNIFTY") == [0.15]
