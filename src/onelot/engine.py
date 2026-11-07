"""Engine runtime: the single long-lived process that owns all state.

*** v1 defect D2 ***
v1 put the engine inside Streamlit. Streamlit re-executes the whole script on
every interaction and every auto-refresh, so module-scope background threads
get DUPLICATED on each rerun — within an hour you have dozens of pollers racing
one broker quota, and button clicks landing mid-rerun are silently dropped.
Dropping the "Trade Taken" click is the single worst failure this system could
have.

So: the engine is a process. It owns the broker connection, the threads, the
quota, the state machine and the risk governor. The UI is a thin client that
reads state over HTTP and POSTs actions. Nothing else may hold strategy state.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from .analytics.ivp import IVPercentileEngine
from .analytics.regime import RegimeModel, compute_for_date
from .broker.base import Contract, TokenExpiredError, nearest_monthly_expiry
from .broker.dhan import DhanAdapter
from .broker.mock import MockBroker
from .clock import IST, SessionClock, TradingCalendar, assert_clock_sane, now_ist, to_ist
from .config import AppConfig, CostConfig, Secrets
from .costs import CostModel, Quote
from .lock import SingleInstanceLock
from .market.chain import ChainCache
from .market.fallback import build_fallback
from .market.feed import INDEX_KEY, FeedContracts, MarketFeed
from .market.health import HealthMonitor
from .notify.manager import Notifier
from .notify.telegram import TelegramClient
from .notify.telegram_channel import TelegramChannel
from .notify.telegram_outbox import TelegramOutbox
from .ratelimit import RateLimiter
from .risk.governor import RiskGovernor
from .store import init_database
from .store.repo import Repo
from .strategy.alpha import AlphaEngine, EvaluationContext
from .trade.guardian import Guardian, GuardianInputs, check_entry_slippage
from .trade.journal import TradeJournal
from .trade.state import ExitReason, TradeStateMachine

if TYPE_CHECKING:  # annotation only: the factory is imported lazily in _paper_book
    from .paper.book import PaperBook

log = logging.getLogger(__name__)


@dataclass
class EngineStatus:
    running: bool = False
    mode: str = "PAPER"
    started_at: dt.datetime | None = None
    trade_date: str | None = None
    expiry: str | None = None
    lot_size: int | None = None
    last_evaluation: dt.datetime | None = None
    last_evaluation_summary: str = ""
    evaluations_today: int = 0
    signals_today: int = 0
    warnings: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, cfg: AppConfig, secrets: Secrets, use_mock: bool = False):
        self.cfg = cfg
        self.secrets = secrets
        self.use_mock = use_mock

        self.lock = SingleInstanceLock(cfg.data_dir / "engine.pid")
        self.limiter = RateLimiter()
        self.cost_model = CostModel(CostConfig.load(cfg.project_root / "config" / "costs.yaml"))
        self.calendar = TradingCalendar.from_yaml(cfg.project_root / "config" / "nse_holidays.yaml")
        self.session_clock = SessionClock(cfg.session, self.calendar)

        self.db = init_database(cfg.db_url_resolved)
        self.repo = Repo(self.db)
        self.journal = TradeJournal(self.repo)

        self.trade_date = now_ist().date().isoformat()
        self.governor = RiskGovernor(
            cfg.risk, cfg.account.capital_inr, cfg.mode, self.trade_date, self.repo
        )
        self.lots_per_trade = cfg.risk.lots_per_trade
        self.notifier = Notifier(cfg, repo=self.repo)
        self.state = TradeStateMachine(self.repo, self.cost_model, cfg, self.governor, self.journal)
        self.alpha = AlphaEngine(cfg, self.cost_model)
        self.guardian = Guardian(cfg, self.cost_model)
        self.ivp_engine = IVPercentileEngine(
            cfg.strategy.volatility.ivp_mode,
            cfg.strategy.volatility.ivp_lookback_days,
            cfg.strategy.volatility.ivp_min_history_days,
        )

        self.health = HealthMonitor(
            socket_dead_after_sec=cfg.data.socket_dead_after_sec,
            quote_stale_after_sec=cfg.data.quote_stale_after_sec,
            chain_stale_after_sec=cfg.data.quote_stale_after_sec,
        )

        # Free, no-credential index source. Backfills history without burning
        # broker quota and gives an INDEPENDENT opinion on the index level --
        # which is the only way to catch a feed that is stuck rather than dead.
        self.fallback = build_fallback(cfg)
        self.broker = None
        self.feed: MarketFeed | None = None
        self.chain: ChainCache | None = None
        self.regime_model: RegimeModel | None = None
        self.current_regime = None
        self.current_ivp = None
        self.expiry: str | None = None
        self.lot_size: int = 0
        self.status = EngineStatus(mode=cfg.mode)

        self.active_signal = None
        self.last_signal_at: dt.datetime | None = None
        self.recent_signal_keys: set[str] = set()
        self._recent_keys_at: dict[str, dt.datetime] = {}

        self._stop = threading.Event()
        # Bars close on two paths (tick-driven and clock-driven). The feed
        # thread signals here; the bar loop does the actual evaluation, so
        # strategy work never blocks the feed.
        self._pending_bar = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._last_chain_flush = 0.0
        self._chain_buffer: list[tuple[dt.datetime, list]] = []
        self._unmanaged_since: dict[str, dt.datetime] = {}
        self._last_rest_quote_at: dict[str, float] = {}

        # Shared services for the web app: the Lab shares this process's
        # database, and the account service reads the free Trading API.
        from .broker.account import AccountService
        from .data.db import use_database
        from .lab.jobs import JobRunner

        use_database(self.db)
        self.account = AccountService(self.db.engine, adapter=None)
        self.lab_jobs = JobRunner(self.db.engine)
        self.desk = None  # built in start(): the multi-instrument Live Desk
        # Phone alerts (US-004): built in start() when notify.telegram is on and configured.
        self.telegram_client: TelegramClient | None = None
        self.telegram_outbox: TelegramOutbox | None = None
        self._last_renew_at: dt.datetime | None = None
        self._last_renew_error: str | None = None

    # ── startup ───────────────────────────────────────────────────────────
    def _paper_book(self) -> PaperBook | None:
        """The paper book for the Live Desk (US-001 AC8), or None when it cannot be built.

        A paper book that fails to build must never stop the desk: it is logged, shown as
        a status warning, and the desk runs without paper trades.
        """
        try:
            from .paper.factory import build_paper_book

            return build_paper_book(self.cfg, self.db.engine)
        except Exception:
            log.exception("paper book failed to build; the Live Desk runs without paper trades")
            self.status.warnings.append("Paper book failed to build — no paper trades; see the log")
            return None

    def start(self) -> None:
        self.lock.acquire()
        # Only the process holding the lock may call the Lab's leftover runs interrupted.
        self.lab_jobs.recover_interrupted()
        self.status.running = True
        self.status.started_at = now_ist()
        self.status.trade_date = self.trade_date
        # First, so even the start-up CRITICALs below reach the phone.
        try:
            self._start_telegram()
        except Exception:
            log.exception("Telegram alerts failed to start (dashboard and OS toast continue)")
            self.status.warnings.append("Telegram alerts failed to start — see the log")

        ok, msg = assert_clock_sane(self.cfg.session.clock_drift_tolerance_sec)
        if not ok:
            self.status.warnings.append(msg)
            self.notifier.critical("Clock problem", msg)

        for problem in self.cfg.blocking_problems():
            self.status.warnings.append(problem)
            log.error("BLOCKING: %s", problem)
        if not self.calendar.verified:
            self.status.warnings.append(
                "NSE holiday calendar is unverified — see config/nse_holidays.yaml"
            )
        if not self.cost_model.cfg.verified_against_contract_note:
            self.status.warnings.append(
                "Cost rates unverified against a real contract note — net P&L "
                "figures are estimates (docs/05_COST_RECONCILIATION.md)"
            )

        self._build_broker()
        self._resolve_instruments()
        self._start_feed()
        self._load_regime()
        recovered = self.state.recover()
        # A recovered position is worthless to the Guardian until its option
        # is actually on the feed. The feed was built above with only the
        # index and the future, so subscribe every open contract now.
        for pos in recovered:
            self._subscribe_position(pos)
        if recovered:
            self.notifier.warning(
                "Recovered open position(s)",
                f"{len(recovered)} position(s) restored from the database and re-subscribed. "
                "Verify them against your broker terminal.",
                dedup_key="recovery",
            )

        try:
            from .data.universe import sync_universe_table

            sync_universe_table(self.db.engine)
        except Exception:
            log.exception("universe table sync failed (Lab dropdowns may be empty)")

        self.notifier.self_test()

        # The Live Desk: every watchlist index/stock under several strategies at
        # once. Runs on Yahoo (delayed) until the Dhan Data API is active, so the
        # whole tab works on day one. Shares the risk governor and the notifier.
        try:
            from .desk import Desk

            self.desk = Desk(
                self.db.engine,
                self.cfg,
                self.notifier,
                self.governor,
                self.session_clock,
                repo=self.repo,
                adapter_getter=lambda: self.account.adapter,
                data_api_active=self._data_api_active,
                feed_getter=lambda: self.feed,
                calendar=self.calendar,
                paper=self._paper_book(),
            )
            self.desk.start()
        except Exception:
            log.exception("Live Desk failed to start (the Bank Nifty engine continues)")
            self.status.warnings.append("Live Desk failed to start — see the log")

        for target, name in (
            (self._bar_loop, "bar-loop"),
            (self._guardian_loop, "guardian-loop"),
            (self._housekeeping_loop, "housekeeping"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        log.info(
            "Engine started in %s mode (broker=%s, expiry=%s, lot=%d)",
            self.cfg.mode,
            getattr(self.broker, "name", "?"),
            self.expiry,
            self.lot_size,
        )

    def _start_telegram(self) -> None:
        """notify.telegram is the master switch (US-004): off, nothing is built; on, the
        channel and its outbox worker are added when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
        are usable, otherwise one INFO line says why (never the values)."""
        if not self.cfg.notify.telegram:
            return
        client = TelegramClient.from_env()
        if not client.available:
            log.info(
                "Telegram not configured (%s); alerts stay on the dashboard and the OS toast",
                client.unavailable_reason,
            )
            client.close()
            return
        outbox = TelegramOutbox(self.db.engine, client)
        self.notifier.add_channel(
            TelegramChannel(client, outbox, self.cfg.notify, self._in_session)
        )
        self.telegram_client, self.telegram_outbox = client, outbox
        outbox.start()
        log.info("Telegram alerts on for %s", ", ".join(self.cfg.notify.telegram_levels))

    def _stop_telegram(self) -> None:
        """Stop and join the outbox worker (before the database goes), then close the client."""
        outbox, self.telegram_outbox = self.telegram_outbox, None
        if outbox is not None:
            outbox.stop()
        client, self.telegram_client = self.telegram_client, None
        if client is not None:
            client.close()

    def _in_session(self, moment: dt.datetime) -> bool:
        """Market hours on a trading day (a holiday is quiet): Telegram's quiet-hours rule."""
        moment = to_ist(moment)  # the trade date is the IST date, whatever zone came in
        window = self.session_clock.current_window(moment)
        return window is not None and window.is_open(moment)

    def _data_api_active(self) -> bool:
        """Cheap: reads the cached profile, never the network."""
        try:
            snap = self.account.snapshot()
            prof = (snap["panels"].get("profile") or {}).get("data") or {}
            if snap.get("token_expired"):
                return False
            return str(prof.get("dataPlan") or "").lower() == "active"
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        log.info("Engine stopping")
        self._stop.set()
        if self.desk:
            self.desk.stop()
        if self.feed:
            self.feed.stop()
        if self.chain:
            self.chain.stop()
        for t in self._threads:
            t.join(timeout=5.0)
        self._stop_telegram()  # joins the outbox worker: it must not outlive the database
        self.db.dispose()
        self.lock.release()
        self.status.running = False

    def _build_broker(self) -> None:
        if self.use_mock or not self.secrets.has_broker_creds:
            if not self.use_mock:
                self.status.warnings.append(
                    "No Dhan credentials found in .env — running on the SYNTHETIC "
                    "mock market. Nothing here reflects the real market."
                )
                log.warning("No credentials; falling back to the mock broker.")
            # Realtime so `--mock` behaves like a live feed end to end.
            self.broker = MockBroker(realtime=True)
            # Free-tier account reads still work on the mock market when
            # credentials exist (e.g. a test token without the Data API).
            if self.secrets.has_broker_creds:
                self.attach_account_adapter()
            return
        from .broker.dhan import DhanAdapter

        self.broker = DhanAdapter(self.secrets.client_id, self.secrets.data_token, self.limiter)
        self.attach_account_adapter()

    def attach_account_adapter(self) -> dict:
        """(Re)build the read-only adapter used for the free Trading-API reads.

        Called at start and whenever credentials are saved from the Settings
        tab. If the market feed is currently the mock, it stays the mock: a
        restart is needed for live DATA, but the ledger/funds/positions panels
        light up immediately.
        """
        from .broker.dhan import DhanAdapter

        if not self.secrets.has_broker_creds:
            self.account.adapter = None
            return {"ok": False, "reason": "no credentials"}
        try:
            adapter = DhanAdapter(self.secrets.client_id, self.secrets.access_token, self.limiter)
        except Exception as exc:  # noqa: BLE001
            self.account.adapter = None
            return {"ok": False, "reason": str(exc)}
        self.account.adapter = adapter
        self.account_adapter = adapter
        return {"ok": True, "feed_is_mock": isinstance(self.broker, MockBroker)}

    def update_credentials(
        self,
        client_id: str | None,
        access_token: str | None,
        data_access_token: str | None,
        pin: str | None = None,
        totp_secret: str | None = None,
    ) -> dict:
        """Persist new Dhan credentials to .env, hot-attach the account adapter,
        refresh the profile, and go LIVE on the spot when the Data API is active."""
        updates = {}
        if client_id is not None:
            updates["DHAN_CLIENT_ID"] = client_id
        if access_token is not None:
            updates["DHAN_ACCESS_TOKEN"] = access_token
        if data_access_token is not None:
            updates["DHAN_DATA_ACCESS_TOKEN"] = data_access_token
        if pin is not None:
            updates["DHAN_PIN"] = pin
        if totp_secret is not None:
            updates["DHAN_TOTP_SECRET"] = totp_secret.replace(" ", "")
        return self._apply_credentials(updates)

    def _apply_credentials(self, updates: dict) -> dict:
        from .config import Secrets
        from .logging_setup import register_secrets

        Secrets.write_env(self.cfg.project_root, updates)
        new = Secrets.load(self.cfg.project_root)
        new.api_token = self.secrets.api_token
        self.secrets = new
        register_secrets(new.secret_values())
        res = self.attach_account_adapter()
        profile_ok = False
        if res.get("ok"):
            # synchronous: the UI (and go_live) need the profile NOW
            refreshed = self.account.refresh(("profile",))
            profile_ok = bool(refreshed.get("profile", {}).get("ok"))
            self.account.on_trade_event()
            if isinstance(self.broker, DhanAdapter):
                self.broker.set_token(new.data_token)
        live = (
            self.go_live()
            if profile_ok
            else {
                "live": not isinstance(self.broker, MockBroker),
                "reason": "profile could not be fetched with this token",
            }
        )
        if self.desk is not None:
            self.desk._wake.set()
        return {
            **res,
            "masked": new.masked(),
            "profile_ok": profile_ok,
            "data_api_active": self._data_api_active(),
            "went_live": bool(live.get("switched")),
            "live": bool(live.get("live")),
            "live_reason": live.get("reason", ""),
            "restart_needed_for_live_data": False,
        }

    def go_live(self) -> dict:
        """Replace the synthetic market with the real Dhan feed, in place.

        Preconditions: credentials, a profile that answers, and the Data API plan
        active on the account. Otherwise the mock keeps running and the reason is
        returned. Nothing here can place an order; "live" means live DATA.
        """
        if not isinstance(self.broker, MockBroker):
            return {"live": True, "switched": False, "reason": "already on the Dhan feed"}
        if not self.secrets.has_broker_creds:
            return {"live": False, "switched": False, "reason": "no credentials"}
        if not self._data_api_active():
            return {
                "live": False,
                "switched": False,
                "reason": "Data API plan is not active on this account (profile.dataPlan); staying on the synthetic market",
            }
        try:
            adapter = DhanAdapter(self.secrets.client_id, self.secrets.data_token, self.limiter)
            adapter.expiry_list(
                self.cfg.instrument.underlying_symbol,
                self.cfg.instrument.index_security_id,
                self.cfg.instrument.exchange_segment_index,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "live": False,
                "switched": False,
                "reason": f"Dhan data check failed: {type(exc).__name__}: {str(exc)[:160]}",
            }
        old_feed, old_chain = self.feed, self.chain
        try:
            if old_feed:
                old_feed.stop()
            if old_chain:
                old_chain.stop()
        except Exception:  # noqa: BLE001
            log.exception("stopping the mock feed")
        self.use_mock = False
        self.broker = adapter
        self.attach_account_adapter()
        self.status.warnings = [
            w for w in self.status.warnings if "SYNTHETIC" not in w and "mock" not in w.lower()
        ]
        try:
            self._resolve_instruments()
            self._start_feed()
            self._load_regime()
        except Exception as exc:  # noqa: BLE001
            log.exception("go_live failed while starting the feed")
            return {
                "live": True,
                "switched": True,
                "reason": f"feed started with errors: {type(exc).__name__}: {str(exc)[:160]}",
            }
        self.notifier.info(
            "Live market data ON",
            "Switched from the synthetic market to the Dhan feed (data only; nothing is traded).",
            dedup_key="go_live",
        )
        log.info("Engine switched to the Dhan feed (expiry=%s, lot=%s)", self.expiry, self.lot_size)
        return {"live": True, "switched": True, "reason": "switched to the Dhan feed"}

    # ── token renewal (TOTP) ──────────────────────────────────────────────
    def renew_token(self, force: bool = False) -> dict:
        """Mint a fresh 24-hour token from the PIN + TOTP secret in .env and apply it everywhere."""
        from .broker.totp import generate_access_token

        if not self.secrets.can_self_renew:
            return {
                "ok": False,
                "reason": "PIN and TOTP secret are not configured (Settings → Dhan credentials)",
            }
        if not force and not self._token_needs_renewal():
            return {
                "ok": True,
                "renewed": False,
                "reason": "token still has more than 90 minutes left",
            }
        try:
            res = generate_access_token(
                self.secrets.client_id, self.secrets.pin, self.secrets.totp_secret
            )
        except Exception as exc:  # noqa: BLE001
            self._last_renew_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.error("token renewal failed: %s", self._last_renew_error)
            return {"ok": False, "reason": self._last_renew_error}
        updates = {"DHAN_ACCESS_TOKEN": res["access_token"]}
        if self.secrets.data_access_token:
            updates["DHAN_DATA_ACCESS_TOKEN"] = res["access_token"]
        out = self._apply_credentials(updates)
        self._last_renew_at = now_ist()
        self._last_renew_error = None
        self.notifier.info(
            "Dhan token renewed",
            f"valid until {res.get('expiry_time') or 'tomorrow'}",
            dedup_key="token_renew",
        )
        return {
            "ok": True,
            "renewed": True,
            "expiry_time": res.get("expiry_time"),
            **{k: out[k] for k in ("live", "data_api_active")},
        }

    def token_expiry(self) -> dt.datetime | None:
        try:
            prof = (self.account.snapshot()["panels"].get("profile") or {}).get("data") or {}
            tv = str(prof.get("tokenValidity") or "")
            return dt.datetime.strptime(tv, "%d/%m/%Y %H:%M").replace(tzinfo=IST) if tv else None
        except Exception:  # noqa: BLE001
            return None

    def _token_needs_renewal(self) -> bool:
        exp = self.token_expiry()
        if exp is None:
            snap = self.account.snapshot()
            return bool(snap.get("token_expired"))
        return (exp - now_ist()) < dt.timedelta(minutes=90)

    def _maybe_auto_renew(self, now: dt.datetime) -> None:
        if not self.secrets.can_self_renew:
            return
        last = getattr(self, "_last_renew_try", None)
        if last and (now - last) < dt.timedelta(minutes=15):
            return
        if self._token_needs_renewal():
            self._last_renew_try = now
            self.renew_token(force=True)

    def _resolve_instruments(self) -> None:
        """Expiry and lot size come from the broker, never from a constant.

        v1 defect D6: both have been revised by regulators. A hardcoded value
        is a landmine that goes off silently, mis-sizing every trade.
        """
        underlying = self.cfg.instrument.underlying_symbol
        today = now_ist().date()

        try:
            expiries = self.broker.expiry_list(underlying)
        except Exception as exc:
            log.error("Could not fetch the expiry list: %s", exc)
            expiries = []
        self.expiry = nearest_monthly_expiry(expiries, today) if expiries else None
        if not self.expiry:
            self.status.warnings.append("could not resolve an expiry from the broker")
            return
        self.status.expiry = self.expiry

        lot = None
        try:
            contracts = self.broker.instruments(underlying)
            self.repo.upsert_instruments(
                [
                    {
                        "security_id": str(c.security_id),
                        "exchange_segment": c.exchange_segment,
                        "symbol": c.symbol,
                        "underlying": c.underlying,
                        "instrument_type": c.instrument_type,
                        "expiry": c.expiry,
                        "strike": c.strike,
                        "opt_type": c.opt_type,
                        "lot_size": c.lot_size,
                        "tick_size": c.tick_size,
                        "refreshed_at": now_ist(),
                    }
                    for c in contracts
                    if c.security_id
                ]
            )
            lots = {
                c.lot_size
                for c in contracts
                if c.expiry == self.expiry and c.opt_type and c.lot_size
            }
            if lots:
                lot = max(lots)
        except Exception as exc:
            log.warning("Instrument master unavailable: %s", exc)

        override = self.cfg.instrument.lot_size_override
        if lot and override and lot != override:
            msg = (
                f"CONFIG DISAGREES WITH THE EXCHANGE: config lot_size_override="
                f"{override} but the scrip master says {lot}. Using {lot}. "
                "Fix config/config.yaml."
            )
            log.error(msg)
            self.status.warnings.append(msg)
        self.lot_size = lot or override or 0
        self.status.lot_size = self.lot_size
        if not self.lot_size:
            self.status.warnings.append("lot size could not be determined — sizing is impossible")

    def _start_feed(self) -> None:
        underlying = self.cfg.instrument.underlying_symbol
        try:
            contracts = self.broker.instruments(underlying)
        except Exception:
            contracts = []
        index_c = next((c for c in contracts if c.instrument_type == "INDEX"), None) or Contract(
            security_id=str(self.cfg.instrument.index_security_id),
            exchange_segment=self.cfg.instrument.exchange_segment_index,
            symbol=underlying,
            underlying=underlying,
            instrument_type="INDEX",
        )
        fut_c = next(
            (c for c in contracts if c.instrument_type == "FUTIDX" and c.expiry == self.expiry),
            None,
        )
        if fut_c is None:
            self.status.warnings.append(
                "no futures contract resolved — VWAP and volume divergence will be "
                "unavailable (the index has no volume)"
            )

        window = self.session_clock.window_for(now_ist().date())
        session_open = (
            window.open_at
            if window
            else now_ist().replace(hour=9, minute=15, second=0, microsecond=0)
        )

        self.feed = MarketFeed(
            self.broker,
            FeedContracts(index=index_c, futures=fut_c),
            self.limiter,
            self.health,
            session_open,
            interval_min=self.cfg.data.bar_interval_min,
            settle_delay_sec=self.cfg.data.bar_settle_delay_sec,
            rest_failover_interval_sec=1.0 / max(self.cfg.data.rest_failover_max_rps, 0.1),
            on_bar_closed=self._on_bar_closed,
        )
        self.health.market_open = window is not None

        try:
            ok, note = self.feed.warm_up(
                self.cfg.data.warmup_sessions, self.calendar, now_ist().date()
            )
            if not ok and self.fallback and self.fallback.available:
                seeded = self.feed.index_series.seed_from_history(
                    self.fallback.intraday_bars(
                        self.cfg.data.bar_interval_min, self.cfg.data.warmup_sessions
                    )
                )
                if seeded:
                    log.info(
                        "Warm-up topped up with %d bars from %s (broker history unavailable)",
                        seeded,
                        self.fallback.name,
                    )
                    ok, note = self.feed.index_series.warmup_status(self.feed._required_bars())
                    self.health.warmup_complete = ok
                    self.health.warmup_note = note if not ok else "warm-up complete"
            if not ok:
                self.status.warnings.append(f"warm-up incomplete: {note}")
        except TokenExpiredError as exc:
            self.status.warnings.append(str(exc))
            self.notifier.critical("Dhan token expired", str(exc))
        except Exception as exc:
            log.warning("warm-up failed: %s", exc)

        self.feed.start()

        if self.expiry:
            self.chain = ChainCache(
                self.broker,
                self.cfg.instrument.underlying_symbol,
                self.expiry,
                poll_interval_sec=self.cfg.data.chain_poll_interval_sec,
                stale_after_sec=self.cfg.data.quote_stale_after_sec,
                greek_tolerance=self.cfg.strategy.contract.greek_cross_check_tol,
                forward_price_fn=self._forward_price,
                on_snapshot=self._on_chain_snapshot,
            )
            self.feed.chain = self.chain
            self.chain.start(self._expiry_dt)

    def _expiry_dt(self) -> dt.datetime:
        d = dt.date.fromisoformat(self.expiry[:10])
        return dt.datetime.combine(d, dt.time(15, 30), tzinfo=IST)

    def _forward_price(self) -> float:
        """Prefer the futures price: index options are priced off the future."""
        f = self.feed.futures_price() if self.feed else None
        if f:
            return f
        return self.feed.index_price() if self.feed else 0.0

    def _on_chain_snapshot(self, snap) -> None:
        self.health.note_chain(now_ist())
        with self._lock:
            self._chain_buffer.append((snap.ts, snap.as_dicts()))

    # ── regime ────────────────────────────────────────────────────────────
    def _load_regime(self) -> None:
        symbol = self.cfg.instrument.underlying_symbol
        today = now_ist().date()
        cached = self.repo.get_regime(today.isoformat(), symbol)
        model_path = self.cfg.data_dir / "regime_model.pkl"
        self.regime_model = (
            RegimeModel.load(model_path) if self.cfg.strategy.regime.engine == "hmm" else None
        )

        rows = self.repo.get_daily_bars(symbol, self.cfg.strategy.regime.train_lookback_days)
        if len(rows) < 60:
            rows = self._backfill_daily(symbol)
        if not rows:
            log.warning("No daily history — regime is UNKNOWN and will block signals")
            return

        dates = [dt.date.fromisoformat(r["trade_date"]) for r in rows]
        closes = np.array([float(r["close"]) for r in rows])
        self.current_regime, self.regime_model = compute_for_date(
            dates,
            closes,
            today,
            model=self.regime_model,
            fallback=self.cfg.strategy.regime.fallback,
            engine=self.cfg.strategy.regime.engine,
        )
        if (
            self.cfg.strategy.regime.engine == "hmm"
            and self.regime_model
            and self.regime_model.is_fitted
        ):
            self.regime_model.save(model_path)
        self.repo.save_regime(
            today.isoformat(),
            symbol,
            self.current_regime.regime.value,
            self.current_regime.confidence,
            self.current_regime.model_hash,
            {},
            now_ist(),
        )
        log.info(
            "Regime for %s: %s (%.2f, %s, as_of=%s)%s",
            today,
            self.current_regime.regime.value,
            self.current_regime.confidence,
            self.current_regime.method,
            self.current_regime.as_of,
            "" if cached is None else " [refit]",
        )

    def _backfill_daily(self, symbol: str) -> list[dict]:
        if not self.feed:
            return []
        today = now_ist().date()
        start = today - dt.timedelta(days=self.cfg.strategy.regime.train_lookback_days * 2)
        try:
            bars = self.broker.historical_daily(self.feed.contracts.index, start, today)
        except Exception as exc:
            log.warning("Broker daily backfill failed: %s", exc)
            bars = []
        if not bars and self.fallback and self.fallback.available:
            bars = self.fallback.daily_bars(self.cfg.strategy.regime.train_lookback_days)
            if bars:
                log.info(
                    "Daily history backfilled from %s (%d bars) — no broker quota used",
                    self.fallback.name,
                    len(bars),
                )
        if not bars:
            return []
        for b in bars:
            self.repo.upsert_daily_bar(
                symbol, b.ts.date().isoformat(), b.open, b.high, b.low, b.close, b.volume
            )
        log.info("Backfilled %d daily bars", len(bars))
        return self.repo.get_daily_bars(symbol, self.cfg.strategy.regime.train_lookback_days)

    # ── main loops ────────────────────────────────────────────────────────
    def _on_bar_closed(self, key: str, bar) -> None:
        """Called on the FEED thread when a tick rolls a bar. Only signals;
        the evaluation itself happens on the bar loop."""
        if key == INDEX_KEY:
            self._pending_bar.set()

    def _bar_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = now_ist()
                # Clock-driven closes, for intervals with no trades at all.
                if self.feed is not None:
                    w = self.session_clock.current_window(now)
                    self.health.market_open = bool(
                        w and w.is_open(now)
                    )  # follows the clock, not just the calendar
                closed = self.feed.tick_time_boundaries(now) if self.feed else []
                if self._pending_bar.is_set() or any(k == INDEX_KEY for k, _ in closed):
                    self._pending_bar.clear()
                    self._evaluate(now)
                self._expire_signals(now)
            except Exception:
                log.exception("bar loop error")
            self._stop.wait(1.0)

    def _evaluate(self, now: dt.datetime) -> None:
        if not self.feed or not self.chain or not self.lot_size:
            return
        window = self.session_clock.current_window(now)
        health = self.feed.health_report(now)

        idx = self.feed.index_series.arrays()
        if len(idx["close"]) < 30:
            return

        atm_iv = self.chain.atm_iv()
        iv_hist = self.repo.atm_iv_series(
            self.cfg.instrument.underlying_symbol,
            self.cfg.strategy.volatility.ivp_lookback_days,
            before_date=self.trade_date,
        )
        self.current_ivp = self.ivp_engine.compute(iv_hist, atm_iv)
        self.health.ivp_usable = self.current_ivp.usable
        self.health.ivp_note = self.current_ivp.note

        allowed, why = self.governor.may_open(now)
        self._prune_recent_keys(now)

        ctx = EvaluationContext(
            now=now,
            bar_ts=self.feed.index_series.latest_closed().ts,
            session_window=window,
            health_may_signal=health.may_signal,
            health_reasons=health.reasons,
            index_high=idx["high"],
            index_low=idx["low"],
            index_close=idx["close"],
            index_open=idx["open"],
            index_ts=idx["ts"],
            chain_rows=self.chain.snapshot.rows if self.chain.snapshot else [],
            chain_age_sec=self.chain.age_seconds(now),
            regime=self.current_regime,
            ivp=self.current_ivp,
            expiry=self.expiry,
            lot_size=self.lot_size,
            risk_allowed=allowed,
            risk_reason=why,
            risk_state=self.governor.state.value,
            open_positions=len(self.state.open_positions),
            last_signal_at=self.last_signal_at,
            recent_signal_keys=set(self.recent_signal_keys),
        )
        outcome = self.alpha.evaluate(ctx)
        self.status.last_evaluation = now
        self.status.last_evaluation_summary = outcome.summary()
        self.status.evaluations_today += 1

        self.repo.record_evaluation(
            outcome.eval_id,
            now,
            ctx.bar_ts,
            outcome.signal.direction if outcome.signal else None,
            outcome.signal is not None,
            outcome.blocked_by,
            {"summary": outcome.summary()},
        )
        self.repo.record_gates(outcome.eval_id, now, None, outcome.all_gate_dicts())

        if outcome.signal:
            self._emit_signal(outcome.signal, now)

    def _emit_signal(self, signal, now: dt.datetime) -> None:
        with self._lock:
            self.active_signal = signal
            self.last_signal_at = now
            self.recent_signal_keys.add(signal.contract_key)
            self._recent_keys_at[signal.contract_key] = now
        self.state.register_signal(signal)
        self.status.signals_today += 1

        if signal.security_id and self.feed:
            self.feed.subscribe_option(
                f"OPT_{signal.contract_key}",
                Contract(
                    security_id=str(signal.security_id),
                    exchange_segment=self.cfg.instrument.exchange_segment_fno,
                    symbol=f"{signal.underlying}{signal.strike:g}{signal.opt_type}",
                    underlying=signal.underlying,
                    instrument_type="OPTIDX",
                    expiry=signal.expiry,
                    strike=signal.strike,
                    opt_type=signal.opt_type,
                    lot_size=self.lot_size,
                ),
            )

        self.notifier.signal(signal)

    def _prune_recent_keys(self, now: dt.datetime) -> None:
        window = dt.timedelta(minutes=self.cfg.strategy.signal.dedup_window_minutes)
        with self._lock:
            stale = [k for k, t in self._recent_keys_at.items() if now - t > window]
            for k in stale:
                self._recent_keys_at.pop(k, None)
                self.recent_signal_keys.discard(k)

    def _expire_signals(self, now: dt.datetime) -> None:
        n = self.state.expire_stale(now)
        if n and self.active_signal and self.active_signal.is_expired(now):
            with self._lock:
                self.active_signal = None

    # ── guardian ──────────────────────────────────────────────────────────
    def _guardian_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = now_ist()
                for pos in list(self.state.open_positions.values()):
                    self._manage(pos, now)
            except Exception:
                log.exception("guardian loop error")
            self._stop.wait(1.0)

    def _position_contract(self, pos) -> Contract | None:
        if not pos.security_id:
            return None
        return Contract(
            security_id=str(pos.security_id),
            exchange_segment=self.cfg.instrument.exchange_segment_fno,
            symbol=f"{pos.underlying}{pos.strike:g}{pos.opt_type}",
            underlying=pos.underlying or self.cfg.instrument.underlying_symbol,
            instrument_type="OPTIDX",
            expiry=pos.expiry,
            strike=pos.strike,
            opt_type=pos.opt_type,
            lot_size=pos.lot_size or self.lot_size,
        )

    def _subscribe_position(self, pos) -> None:
        c = self._position_contract(pos)
        if c is None or not self.feed:
            log.error(
                "Position %s has no security_id — the Guardian cannot quote it; "
                "manage it manually.",
                pos.position_id,
            )
            return
        self.feed.subscribe_option(f"OPT_{pos.expiry}_{pos.strike:g}_{pos.opt_type}", c)

    def _handle_unmanaged(self, pos, now: dt.datetime) -> None:
        """The option of an OPEN position has no usable quote.

        This is the failure the old build swallowed silently. Escalation:
          1. pull a REST quote through the limiter (once per few seconds);
          2. past `quote_stale_after_sec`, mark the position degraded and warn;
          3. past 3x that, raise CRITICAL every minute until data returns.
        A losing feed must never mean a silently abandoned trade.
        """
        first = self._unmanaged_since.setdefault(pos.position_id, now)
        age = (now - first).total_seconds()
        stale = self.cfg.data.quote_stale_after_sec
        last_rest = self._last_rest_quote_at.get(pos.position_id, 0.0)
        if time.monotonic() - last_rest >= 3.0 and self.feed:
            self._last_rest_quote_at[pos.position_id] = time.monotonic()
            c = self._position_contract(pos)
            if c is not None:
                self.feed.request_quote(c)
        if age >= stale and not pos.degraded_data:
            pos.degraded_data = True
            self.repo.update_position(pos.position_id, degraded_data=True)
            self.notifier.warning(
                "Position UNMANAGED — no option quote",
                f"{pos.strike:g} {pos.opt_type}: no valid bid/ask for {age:.0f}s. The stop "
                f"({pos.effective_stop:.2f}) cannot be watched. Watch it on your terminal.",
                dedup_key=f"unmanaged:{pos.position_id}",
            )
        if age >= stale * 3:
            bucket = int(age // 60)
            self.notifier.critical(
                "Position still UNMANAGED",
                f"{pos.strike:g} {pos.opt_type} has had no usable quote for {age / 60:.0f} min. "
                "Consider exiting on your terminal.",
                dedup_key=f"unmanaged-critical:{pos.position_id}:{bucket}",
            )

    def _manage(self, pos, now: dt.datetime) -> None:
        if not self.feed:
            return
        q = self.feed.quote_for(pos.security_id) if pos.security_id else None
        quote = Quote(bid=q.bid or 0.0, ask=q.ask or 0.0, ltp=q.ltp or 0.0) if q else None
        if quote is None or not quote.is_valid:
            self._handle_unmanaged(pos, now)
            return
        if pos.position_id in self._unmanaged_since:
            self._unmanaged_since.pop(pos.position_id, None)
            if pos.degraded_data:
                pos.degraded_data = False
                self.repo.update_position(pos.position_id, degraded_data=False)
                self.notifier.info(
                    "Position quote restored",
                    f"{pos.strike:g} {pos.opt_type} is being managed again.",
                    dedup_key=f"restored:{pos.position_id}",
                )

        idx = self.feed.index_series.arrays()
        fut = self.feed.futures_series.session_arrays(now.date())
        health = self.feed.health_report(now)

        decision = self.guardian.evaluate(
            pos,
            GuardianInputs(
                now=now,
                quote=quote,
                index_high=idx["high"],
                index_low=idx["low"],
                index_close=idx["close"],
                futures_high=fut["high"],
                futures_low=fut["low"],
                futures_close=fut["close"],
                futures_volume=fut["volume"],
                session_window=self.session_clock.current_window(now),
                regime_direction=(
                    self.current_regime.regime.value if self.current_regime else None
                ),
                data_degraded=not health.may_signal,
            ),
        )

        if decision.trail_stop is not None and (
            pos.trail_stop is None
            or decision.trail_stop > pos.trail_stop
            or decision.trail_active != pos.trail_active
        ):
            self.state.update_trail(
                pos.position_id,
                decision.trail_stop,
                decision.trail_active,
                decision.high_water or pos.entry_price,
            )

        if decision.is_exit:
            self.state.request_exit(pos.position_id, decision.reason)
            self.notifier.exit_alert(pos, decision)

    # ── housekeeping ──────────────────────────────────────────────────────
    def _housekeeping_loop(self) -> None:
        last_iv_record = ""
        last_crosscheck = 0.0
        while not self._stop.is_set():
            try:
                now = now_ist()
                self._flush_chain(now)

                # Independent index cross-check. A feed that is connected but
                # STUCK returns a plausible number forever and passes every
                # staleness test; only a second source exposes it.
                # (Skipped on the synthetic market: the mock index is not Bank
                # Nifty, so a divergence there is noise, not a warning.)
                if (
                    self.fallback
                    and self.fallback.available
                    and not isinstance(self.broker, MockBroker)
                    and time.monotonic() - last_crosscheck
                    >= self.cfg.data.fallback_poll_interval_sec
                ):
                    last_crosscheck = time.monotonic()
                    ok, why = self.fallback.check_divergence(
                        self.feed.index_price() if self.feed else None, now
                    )
                    if not ok:
                        log.error("INDEX DIVERGENCE: %s", why)
                        self.notifier.critical("Index feed disagreement", why)
                        self.health.note_api_error(why)
                self._maybe_auto_renew(now)
                today = now.date().isoformat()
                if today != last_iv_record and self.chain and self.chain.snapshot:
                    iv = self.chain.atm_iv()
                    if iv and not np.isnan(iv):
                        fwd = self._forward_price()
                        self.repo.record_atm_iv(
                            today, self.cfg.instrument.underlying_symbol, float(iv), None, fwd, now
                        )
                        last_iv_record = today
            except Exception:
                log.exception("housekeeping error")
            self._stop.wait(10.0)

    def _flush_chain(self, now: dt.datetime) -> None:
        if time.monotonic() - self._last_chain_flush < self.cfg.data.chain_db_flush_interval_sec:
            return
        with self._lock:
            buf, self._chain_buffer = self._chain_buffer, []
        for ts, rows in buf:
            try:
                self.repo.record_chain_snapshot(
                    ts, self.cfg.instrument.underlying_symbol, self.expiry or "", rows
                )
            except Exception:
                log.exception("chain flush failed")
        self._last_chain_flush = time.monotonic()

    # ── public actions (called by the API) ────────────────────────────────
    def acknowledge(
        self, signal_id: str, action: str, source: str, fill_price: float | None = None
    ) -> dict:
        now = now_ist()
        result = self.state.acknowledge(signal_id, action, source, fill_price, now)
        if result.accepted and result.position is not None:
            sig_row = self.repo.get_signal(signal_id)
            q = self.feed.quote_for(result.position.security_id) if self.feed else None
            if q and sig_row:
                check = check_entry_slippage(
                    float(sig_row["quote_ask"] or sig_row["entry_eff"]),
                    Quote(bid=q.bid or 0.0, ask=q.ask or 0.0, ltp=q.ltp or 0.0),
                    self.cfg.execution_assumptions.slippage_gate_pct,
                    self.cfg.execution_assumptions.slippage_gate_spread_mult,
                )
                if not check.within_tolerance:
                    self.notifier.warning("Entry slippage", check.message)
                    result.position.payload["slippage"] = check.as_dict()
            with self._lock:
                if self.active_signal and self.active_signal.signal_id == signal_id:
                    self.active_signal = None
            # The account changed: refresh funds/positions once, never poll.
            self.account.on_trade_event()
        elif result.status is not None:
            with self._lock:
                if self.active_signal and self.active_signal.signal_id == signal_id:
                    self.active_signal = None
        return result.as_dict()

    def close_position(
        self, position_id: str, exit_price: float, reason: str = "MANUAL"
    ) -> dict | None:
        try:
            r = ExitReason(reason)
        except ValueError:
            r = ExitReason.MANUAL
        out = self.state.confirm_exit(position_id, exit_price, r, now_ist())
        if out is not None:
            self._unmanaged_since.pop(position_id, None)
            self.account.on_trade_event()
        return out

    def panic_halt(self) -> dict:
        self.governor.manual_panic_halt(now_ist())
        self.notifier.critical(
            "MANUAL HALT", "Trading halted by operator. No new signals will fire."
        )
        return self.governor.snapshot().as_dict()

    def reset_halt(self) -> dict:
        self.governor.reset_halt(now_ist())
        return self.governor.snapshot().as_dict()

    # ── state for the UI ──────────────────────────────────────────────────
    def snapshot(self) -> dict:
        now = now_ist()
        health = self.feed.health_report(now) if self.feed else None
        window = self.session_clock.current_window(now)
        return {
            "status": {
                "running": self.status.running,
                "mode": self.cfg.mode,
                "broker": getattr(self.broker, "name", None),
                "started_at": (
                    self.status.started_at.isoformat() if self.status.started_at else None
                ),
                "now": now.isoformat(),
                "trade_date": self.trade_date,
                "expiry": self.expiry,
                "lot_size": self.lot_size,
                "capital": self.cfg.account.capital_inr,
                "evaluations_today": self.status.evaluations_today,
                "signals_today": self.status.signals_today,
                "last_evaluation": (
                    self.status.last_evaluation.isoformat() if self.status.last_evaluation else None
                ),
                "last_evaluation_summary": self.status.last_evaluation_summary,
                "warnings": list(self.status.warnings),
                "session": {
                    "is_trading_day": window is not None,
                    "entries_allowed": bool(window and window.entries_allowed(now)),
                    "squareoff_at": (
                        window.mandatory_squareoff_at.strftime("%H:%M") if window else None
                    ),
                }
                if True
                else {},
            },
            "health": health.as_dict() if health else None,
            "regime": self.current_regime.as_dict() if self.current_regime else None,
            "ivp": self.current_ivp.as_dict() if self.current_ivp else None,
            "risk": self.governor.snapshot().as_dict(),
            "active_signal": (
                self.active_signal.as_dict()
                if self.active_signal and not self.active_signal.is_expired(now)
                else None
            ),
            "signal_seconds_remaining": (
                self.active_signal.seconds_remaining(now) if self.active_signal else 0
            ),
            "positions": [p.as_dict() for p in self.state.open_positions.values()],
            "prices": {
                "index": self.feed.index_price() if self.feed else None,
                "futures": self.feed.futures_price() if self.feed else None,
            },
            "chain": self.chain.stats_dict() if self.chain else None,
            "fallback": self.fallback.status() if self.fallback else None,
            "rate_limiter": self.limiter.snapshot(),
            "notifications": self.notifier.status(),
            "journal": self.journal.metrics(self.cfg.mode),
            "desk": self.desk.snapshot() if self.desk else None,
        }

    def gate_histogram(self, hours: int = 24) -> list[dict]:
        since = now_ist() - dt.timedelta(hours=hours)
        return self.repo.gate_rejection_histogram(since)
