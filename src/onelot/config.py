"""Configuration: strict, validated, fail-fast.

Secrets come from the environment (`.env`); everything else comes from YAML.
Nothing is read ad-hoc anywhere else in the codebase — if it isn't modelled
here, it isn't configurable, and a typo in a YAML key is a startup error
rather than a silently-ignored setting.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .clock import parse_hhmm

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Strict(BaseModel):
    """Reject unknown keys everywhere. A misspelled option must not be silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ─────────────────────────── costs ────────────────────────────────────────


class BrokerageCfg(Strict):
    mode: Literal["flat", "min_of"] = "flat"
    per_order_inr: float = 20.0
    pct_of_turnover: float = 0.0003
    cap_inr: float = 20.0


class TaxCfg(Strict):
    stt_buy_pct: float = 0.0  # delivery equity only; everything else 0
    stt_sell_pct: float
    stt_exercise_pct: float = 0.0
    exchange_txn_pct: float
    sebi_turnover_pct: float
    ipft_pct: float
    stamp_duty_pct: float
    gst_pct: float


class ProductCostCfg(Strict):
    """Charges for one product (index_options, equity_intraday, ...)."""

    brokerage: BrokerageCfg = BrokerageCfg()
    taxes: TaxCfg


# Cost product used for each tradeable category in the Backtest Lab.
PRODUCT_FOR_CATEGORY = {
    "STOCK": "equity_intraday",
    "FUTURES": "index_futures",
    "STOCK_FUTURES": "stock_futures",
    "OPTIONS": "index_options",
    "STOCK_OPTIONS": "stock_options",
    "CURRENCY": "currency_futures",
}


class CostConfig(Strict):
    effective_from: str
    verified_against_contract_note: bool = False
    currency: str = "INR"
    brokerage: BrokerageCfg
    taxes: TaxCfg
    # Per-product tables. The top-level brokerage/taxes remain the default
    # (NSE index options -- the live engine's product).
    products: dict[str, ProductCostCfg] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> CostConfig:
        data = yaml.safe_load(Path(path).read_text())
        obj = cls(**data)
        if not obj.verified_against_contract_note:
            log.warning(
                "Cost rates are UNVERIFIED (effective_from=%s). Reconcile against a real "
                "contract note (docs/05_COST_RECONCILIATION.md) before trusting net P&L.",
                obj.effective_from,
            )
        return obj

    def product(self, name: str | None) -> CostConfig:
        """A CostConfig whose brokerage/taxes are those of `name`.

        Unknown product names fall back to the default table rather than
        raising: a backtest must never silently run cost-free, and the
        default (options) table is the most expensive one, so the error is
        conservative.
        """
        if not name or name not in self.products:
            if name:
                log.warning("cost product %r not in costs.yaml; using the default table", name)
            return self
        p = self.products[name]
        return self.model_copy(update={"brokerage": p.brokerage, "taxes": p.taxes})


# ─────────────────────────── main config ──────────────────────────────────


class AccountCfg(Strict):
    # OPTIONAL. Nothing depends on it -- the one-lot rule means the engine
    # never needs your account size, and risk limits are absolute rupees.
    # Set it only if you want percentage figures on the dashboard and a
    # concentration warning. Leave at 0 to keep it private.
    capital_inr: float = 0.0
    currency: str = "INR"


class InstrumentCfg(Strict):
    underlying_symbol: str = "BANKNIFTY"
    index_security_id: int = 25
    exchange_segment_index: str = "IDX_I"
    exchange_segment_fno: str = "NSE_FNO"
    expiry_selection: Literal["nearest_monthly", "nearest"] = "nearest_monthly"
    lot_size_override: int | None = None
    tick_size: float = 0.05
    freeze_qty: int = 900


class SessionCfg(Strict):
    timezone: str = "Asia/Kolkata"
    market_open: dt.time = Field(default=dt.time(9, 15))
    market_close: dt.time = Field(default=dt.time(15, 30))
    first_entry_after: dt.time = Field(default=dt.time(9, 30))
    no_new_entry_after: dt.time = Field(default=dt.time(14, 45))
    eod_advisory: dt.time = Field(default=dt.time(15, 10))
    mandatory_squareoff: dt.time = Field(default=dt.time(15, 15))
    clock_drift_tolerance_sec: float = 5.0

    @field_validator(
        "market_open",
        "market_close",
        "first_entry_after",
        "no_new_entry_after",
        "eod_advisory",
        "mandatory_squareoff",
        mode="before",
    )
    @classmethod
    def _parse_time(cls, v):
        return parse_hhmm(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _ordering(self):
        seq = [
            ("market_open", self.market_open),
            ("first_entry_after", self.first_entry_after),
            ("no_new_entry_after", self.no_new_entry_after),
            ("eod_advisory", self.eod_advisory),
            ("mandatory_squareoff", self.mandatory_squareoff),
            ("market_close", self.market_close),
        ]
        for (n1, t1), (n2, t2) in zip(seq, seq[1:], strict=False):
            if t1 > t2:
                raise ValueError(f"session times out of order: {n1}={t1} > {n2}={t2}")
        return self


class DataCfg(Strict):
    # Historical corpus for the Backtest Lab.
    #   auto  = Dhan when credentials + Data API are present, else Yahoo
    #   dhan  = Dhan only (5 years of 1-minute bars; needs the Data API)
    #   yahoo = keyless (2m/5m/15m ~60 days, 60m ~2 years, daily unlimited)
    provider: Literal["auto", "dhan", "yahoo"] = "auto"
    history_start: str = "2017-01-01"
    corpus_resolutions: list[str] = Field(default_factory=lambda: ["1d", "60m", "15m", "5m", "2m"])

    chain_poll_interval_sec: float = 5.0
    chain_db_flush_interval_sec: float = 60.0
    socket_ping_interval_sec: float = 5.0
    socket_dead_after_sec: float = 12.0
    quote_stale_after_sec: float = 20.0
    rest_failover_max_rps: float = 1.0
    warmup_sessions: int = 5
    bar_interval_min: int = 5
    bar_settle_delay_sec: float = 2.0

    # Free, no-credential backup source for INDEX data only. It cannot supply
    # an option chain, greeks or option quotes, so it can never replace the
    # broker for a trading decision — see market/fallback.py.
    fallback_source: Literal["yfinance", "none"] = "yfinance"
    fallback_poll_interval_sec: float = 300.0
    fallback_divergence_pct: float = 0.005

    @model_validator(mode="after")
    def _rate_limit_guard(self):
        # Dhan's option-chain endpoint permits 1 request per 3 seconds.
        if self.fallback_poll_interval_sec < 60.0:
            raise ValueError(
                "fallback_poll_interval_sec below 60s: the fallback is a safety "
                "net, not a data feed. Polling a free public endpoint faster "
                "than once a minute invites a block and buys nothing."
            )
        if self.chain_poll_interval_sec < 3.0:
            raise ValueError(
                f"chain_poll_interval_sec={self.chain_poll_interval_sec} breaches the "
                "1-request-per-3-seconds option chain limit and will trigger RL001."
            )
        return self


class RegimeCfg(Strict):
    enabled: bool = True
    # rule_based: EMA21 vs EMA50 plus slope. ~15 lines, no dependencies,
    #   nothing to fit, nothing to persist, no look-ahead surface.
    # hmm: the 3-state Gaussian model. Statistically richer, but pulls in
    #   hmmlearn + scikit-learn and refits daily. Install them yourself:
    #     pip install -r requirements-hmm.txt
    engine: Literal["rule_based", "hmm"] = "rule_based"
    n_states: int = 3
    train_lookback_days: int = 750
    min_state_confidence: float = 0.60
    fallback: Literal["rule_based", "block", "allow"] = "rule_based"


class IntradayChopCfg(Strict):
    adx_min: float = 20.0
    atr_percentile_min: float = 20.0
    atr_percentile_max: float = 90.0


class TrendCfg(Strict):
    ema_period: int = 21


class MomentumCfg(Strict):
    rsi_period: int = 14
    ce_band: tuple[float, float] = (55.0, 65.0)
    pe_band: tuple[float, float] = (35.0, 45.0)


class VolatilityCfg(Strict):
    ivp_max: float = 45.0
    ivp_lookback_days: int = 252
    ivp_min_history_days: int = 60
    ivp_mode: Literal["strict", "degraded", "off"] = "strict"


class ContractCfg(Strict):
    delta_min: float = 0.60
    delta_max: float = 0.70
    # Liquidity sanity gate: spread <= max(spread_abs_floor, spread_pct_max * mid).
    # `spread_abs_floor` is a FLOOR (tick-granularity allowance on cheap
    # options), not a ceiling. The spread's true economic cost is priced into
    # rr_net via ask-entry / bid-exit, not policed here.
    spread_abs_floor: float = 1.0
    spread_pct_max: float = 0.02
    min_bid_qty_lots: int = 2
    min_open_interest: int = 50_000
    min_volume: int = 1_000
    greek_cross_check_tol: float = 0.08

    def max_allowed_spread(self, mid: float) -> float:
        return max(self.spread_abs_floor, self.spread_pct_max * mid)

    @model_validator(mode="after")
    def _delta_positive(self):
        # v1 defect A8: put deltas are negative. We compare against ABS(delta),
        # so the configured bounds must be positive or no PE ever matches.
        if self.delta_min <= 0 or self.delta_max <= 0:
            raise ValueError("delta bounds are compared against ABS(delta) and must be positive")
        if self.delta_min >= self.delta_max:
            raise ValueError("delta_min must be < delta_max")
        return self


class ThetaCfg(Strict):
    expected_hold_hours: float = 1.5
    max_theta_burn_pct: float = 0.10


class ExitCfg(Strict):
    atr_period: int = 5
    sl_atr_mult: float = 1.5
    rr_target_gross: float = 2.5
    rr_net_min: float = 2.0
    trail_activate_r: float = 1.0
    time_stop_minutes: int = 45

    @model_validator(mode="after")
    def _rr_coherent(self):
        # v1 defect A6: you cannot demand a NET R/R at or above the GROSS
        # target multiple — costs make it unreachable, so the gate would
        # reject every signal forever.
        if self.rr_net_min >= self.rr_target_gross:
            raise ValueError(
                f"rr_net_min={self.rr_net_min} >= rr_target_gross={self.rr_target_gross}: "
                "net R/R is always below gross, so no signal could ever pass."
            )
        return self


class GuardianCfg(Strict):
    adx_period: int = 14
    adx_hold_min: float = 25.0
    vwap_source: Literal["futures", "option"] = "futures"
    divergence_volume_drop_pct: float = 0.20

    @model_validator(mode="after")
    def _no_index_vwap(self):
        # v1 defect B3: the NIFTY BANK index is a computed number with no
        # traded volume. VWAP on it is 0/0. Only volume-bearing series allowed.
        if self.vwap_source not in ("futures", "option"):
            raise ValueError("VWAP requires a series with real volume; the index has none")
        return self


class SignalCfg(Strict):
    ttl_seconds: int = 75
    cooldown_minutes: int = 15
    dedup_window_minutes: int = 30


class LiveAlphaCfg(Strict):
    """Where the live engine's DIRECTION comes from.

    gates = the built-in Bank Nifty gate stack (regime + EMA trend + RSI band
            + chop filter + IV percentile).
    lab   = a Backtest Lab strategy (ema_cross, supertrend_vol, ...) evaluated
            on the closed index bars. The health, session, contract-selection,
            net-R/R and risk-governor gates still apply on top of it, so the
            Lab strategy only ever supplies the direction and the stop.
    """

    source: Literal["gates", "lab"] = "gates"
    lab_strategy: str = "ema_cross"
    lab_params: dict = Field(default_factory=dict)
    lab_timeframe: str = "5m"


class StrategyCfg(Strict):
    live: LiveAlphaCfg = LiveAlphaCfg()
    regime: RegimeCfg = RegimeCfg()
    intraday_chop: IntradayChopCfg = IntradayChopCfg()
    trend: TrendCfg = TrendCfg()
    momentum: MomentumCfg = MomentumCfg()
    volatility: VolatilityCfg = VolatilityCfg()
    contract: ContractCfg = ContractCfg()
    theta: ThetaCfg = ThetaCfg()
    exits: ExitCfg = ExitCfg()
    guardian: GuardianCfg = GuardianCfg()
    signal: SignalCfg = SignalCfg()


class BacktestCfg(Strict):
    """Backtest Lab defaults. Every backtest is ONE unit per trade: one share
    for stocks, one lot for futures/options/currency."""

    slippage_pct: float = 0.03  # per side, % of price (stocks, futures, FX)
    option_slippage_ticks: int = 2  # per side, in ticks, on the option premium
    default_timeframe: str = "5m"
    warmup_sessions: int = 3
    use_fine_path: bool = True  # resolve same-bar stop/target with finer bars
    # Denominator for percentage metrics (return %, drawdown %) only. The
    # one-lot rule means no sizing depends on it.
    reference_capital_inr: float = 100000.0
    # Synthetic option pricing (until Dhan's expired-options data is available):
    # Black-76 on the underlying with IV from India VIX (x a scale) or a constant.
    option_iv_source: Literal["vix", "constant"] = "vix"
    option_iv_mult: float = 1.15  # Bank Nifty ATM IV runs ~1.1-1.3x India VIX
    option_iv_constant: float = 0.15
    option_target_delta: float = 0.65  # sufficiently ITM, like the live engine
    option_expiry_weekday: int = 1  # 0=Mon .. 4=Fri; NSE monthly index expiry (verify)
    option_min_days_to_expiry: int = 3  # roll to the next month inside this window
    option_spread_pct: float = 0.006  # half-spread as a fraction of premium (synthetic fills)
    risk_free_rate: float = 0.065

    @model_validator(mode="after")
    def _sane(self):
        if not 0.3 <= self.option_target_delta <= 0.9:
            raise ValueError("option_target_delta must be between 0.30 and 0.90")
        if self.reference_capital_inr <= 0:
            raise ValueError("reference_capital_inr must be positive")
        return self


class RiskCfg(Strict):
    """Risk control for a one-lot system.

    THE RULE: exactly one lot per trade, always, whatever it costs. One lot is
    affordable by definition, so there is no affordability gate, no position
    sizing, and no concept of a signal being "too expensive". Sizing is not a
    decision this system makes.

    Risk is therefore controlled by two things only:

      1. The STOP, which bounds the loss on any single trade. Extreme
         volatility is already excluded upstream by the intraday chop filter
         (`atr_percentile_max`), so there is no separate rupee cap per trade.
      2. ABSOLUTE RUPEE LIMITS on the day and on cumulative drawdown.

    Limits are in rupees rather than percentages on purpose: with a fixed lot
    the loss on a trade is a rupee amount set by the market, not a percentage
    of anything, and rupee limits mean the engine never needs to know your
    account size.
    """

    # Informational only. Used to display percentages and to warn about
    # concentration. Leave at 0 to keep it private -- nothing depends on it.
    lots_per_trade: int = 1

    # Stop trading for the day once realised losses reach this.
    daily_loss_limit_inr: float = 6000.0

    # Cumulative peak-to-trough drawdown across ALL sessions. The backstop the
    # daily limit cannot provide: a run of days each inside the daily limit
    # still adds up, and nothing else would object. Needs a manual reset.
    max_drawdown_inr: float = 18000.0

    max_trades_per_day: int = 2
    max_consecutive_losses: int = 2
    max_open_positions: int = 1

    # Advisory only: warn when one lot's premium exceeds this share of the
    # capital you (optionally) declared. Never blocks -- the one-lot rule wins.
    deployment_warn_pct: float = 0.50

    @model_validator(mode="after")
    def _sane(self):
        if self.lots_per_trade < 1:
            raise ValueError("lots_per_trade must be >= 1")
        if self.daily_loss_limit_inr <= 0:
            raise ValueError("daily_loss_limit_inr must be positive")
        if self.max_drawdown_inr < self.daily_loss_limit_inr:
            raise ValueError(
                f"max_drawdown_inr ({self.max_drawdown_inr:,.0f}) is below "
                f"daily_loss_limit_inr ({self.daily_loss_limit_inr:,.0f}): a "
                "single normal day would trip the cumulative breaker"
            )
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if self.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be >= 1")
        return self


class ExecAssumptionsCfg(Strict):
    slippage_ticks_entry: int = 2
    slippage_ticks_exit: int = 2
    slippage_gate_pct: float = 0.03
    slippage_gate_spread_mult: float = 1.5


class StorageCfg(Strict):
    db_url: str | None = None
    retention_days_quotes: int = 30
    retention_days_chain: int = 90


class ApiCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8787
    allow_non_loopback: bool = False

    @model_validator(mode="after")
    def _loopback(self):
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.allow_non_loopback:
            raise ValueError(
                f"api.host={self.host} is not loopback. Exposing the engine on the network "
                "reveals your positions and lets anyone acknowledge signals. Set "
                "api.allow_non_loopback: true only if you genuinely intend this."
            )
        return self


class DashboardCfg(Strict):
    refresh_ms: int = 1000
    sound_enabled: bool = True
    # Visual theme of the built-in web app.
    theme: Literal["shonen", "plain"] = "shonen"


class NotifyCfg(Strict):
    os_toast: bool = True
    dashboard: bool = True
    telegram: bool = False
    webhook: bool = False


class LoggingCfg(Strict):
    level: str = "INFO"
    dir: str = "logs"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 10


class AppConfig(Strict):
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    account: AccountCfg = AccountCfg()
    instrument: InstrumentCfg = InstrumentCfg()
    session: SessionCfg = SessionCfg()
    data: DataCfg = DataCfg()
    strategy: StrategyCfg = StrategyCfg()
    risk: RiskCfg = RiskCfg()
    execution_assumptions: ExecAssumptionsCfg = ExecAssumptionsCfg()
    backtest: BacktestCfg = BacktestCfg()
    storage: StorageCfg = StorageCfg()
    api: ApiCfg = ApiCfg()
    dashboard: DashboardCfg = DashboardCfg()
    notify: NotifyCfg = NotifyCfg()
    logging: LoggingCfg = LoggingCfg()

    # populated at load time, not from YAML
    project_root: Path = PROJECT_ROOT

    @classmethod
    def load(cls, path: str | Path | None = None, project_root: Path | None = None) -> AppConfig:
        root = project_root or PROJECT_ROOT
        path = Path(path or os.environ.get("ONELOT_CONFIG") or root / "config" / "config.yaml")
        if not path.is_absolute():
            path = root / path
        data = yaml.safe_load(path.read_text()) or {}
        data["project_root"] = root
        return cls(**data)

    # ── derived paths (all quoted-safe; the project path may contain spaces) ──
    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def log_dir(self) -> Path:
        return self.project_root / self.logging.dir

    @property
    def db_url_resolved(self) -> str:
        env = os.environ.get("DB_URL", "").strip()
        if env:
            return env
        if self.storage.db_url:
            return self.storage.db_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.data_dir / 'onelot.db'}"

    @property
    def capital_configured(self) -> bool:
        return self.account.capital_inr > 0

    def blocking_problems(self) -> list[str]:
        """Reasons the engine must refuse to emit signals. Checked every cycle.

        Capital is deliberately NOT here: with one fixed lot there is nothing
        to size, so an undeclared account size cannot make the engine unsafe.
        """
        return []


# ─────────────────────────── secrets ──────────────────────────────────────


class Secrets:
    """Credentials, loaded from the environment (`.env`) only. Never logged.

    Two Dhan tokens are supported because the operator keeps two: a free-tier
    "test" token and the paid Data-API token. Dhan's Data API entitlement is
    on the ACCOUNT, not the token, so in practice either token works for
    everything once the subscription is active. The split is kept so the
    engine can prefer `DHAN_DATA_ACCESS_TOKEN` for data endpoints when both
    are present and fall back cleanly when only one is.
    """

    def __init__(
        self,
        client_id: str = "",
        access_token: str = "",
        api_token: str = "",
        data_access_token: str = "",
        pin: str = "",
        totp_secret: str = "",
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.data_access_token = data_access_token
        self.api_token = api_token
        # Optional: lets the engine mint its own 24-hour tokens (TOTP enabled on Dhan).
        self.pin = pin
        self.totp_secret = totp_secret

    @property
    def can_self_renew(self) -> bool:
        return bool(self.client_id and self.pin and self.totp_secret)

    @property
    def data_token(self) -> str:
        """The token to use for Data-API calls (paid token if set, else the primary)."""
        return self.data_access_token or self.access_token

    @classmethod
    def load(cls, project_root: Path | None = None) -> Secrets:
        root = project_root or PROJECT_ROOT
        env_path = root / ".env"
        if env_path.exists():
            try:
                from dotenv import load_dotenv

                load_dotenv(env_path, override=False)
            except ImportError:  # pragma: no cover
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        api_token = os.environ.get("ONELOT_API_TOKEN", "").strip()
        if not api_token:
            api_token = cls._ephemeral_api_token(root)
        return cls(
            client_id=os.environ.get("DHAN_CLIENT_ID", "").strip(),
            access_token=os.environ.get("DHAN_ACCESS_TOKEN", "").strip(),
            api_token=api_token,
            data_access_token=os.environ.get("DHAN_DATA_ACCESS_TOKEN", "").strip(),
            pin=os.environ.get("DHAN_PIN", "").strip(),
            totp_secret=os.environ.get("DHAN_TOTP_SECRET", "").strip(),
        )

    ENV_KEYS = (
        "DHAN_CLIENT_ID",
        "DHAN_ACCESS_TOKEN",
        "DHAN_DATA_ACCESS_TOKEN",
        "DHAN_PIN",
        "DHAN_TOTP_SECRET",
    )

    @classmethod
    def write_env(cls, project_root: Path, updates: dict[str, str]) -> Path:
        """Persist credential updates into `.env` (mode 0600), keeping every
        other line intact. Values are also exported into this process so the
        running engine can pick them up without a restart. Nothing is echoed."""
        root = project_root or PROJECT_ROOT
        path = root / ".env"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k in updates:
                    out.append(f"{k}={updates[k].strip()}")
                    seen.add(k)
                    continue
            out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v.strip()}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - Windows
            pass
        for k, v in updates.items():
            if v.strip():
                os.environ[k] = v.strip()
            else:
                os.environ.pop(k, None)
        return path

    def masked(self) -> dict:
        """What the Settings tab may show: presence and the last 4 characters only."""

        def m(v: str) -> str | None:
            return (f"…{v[-4:]}" if len(v) >= 8 else "set") if v else None

        return {
            "client_id": m(self.client_id),
            "access_token": m(self.access_token),
            "data_access_token": m(self.data_access_token),
            "has_broker_creds": self.has_broker_creds,
            "pin_set": bool(self.pin),
            "totp_set": bool(self.totp_secret),
            "can_self_renew": self.can_self_renew,
        }

    @staticmethod
    def _ephemeral_api_token(root: Path) -> str:
        """The engine<->dashboard shared secret. Persisted in data/api_token.txt
        and REUSED across restarts: regenerating it every start broke every
        page that was already open ("invalid or missing API token" on Save)."""
        d = root / "data"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "api_token.txt"
        if f.exists():
            existing = f.read_text().strip()
            if len(existing) >= 32:
                return existing
        tok = secrets.token_urlsafe(32)
        f.write_text(tok)
        try:
            f.chmod(0o600)
        except OSError:  # pragma: no cover - Windows
            pass
        return tok

    @property
    def has_broker_creds(self) -> bool:
        return bool(self.client_id and self.access_token)

    def secret_values(self) -> list[str]:
        """Everything the log redactor must scrub. See logging_setup.py."""
        return [
            v
            for v in (
                self.client_id,
                self.access_token,
                self.data_access_token,
                self.api_token,
                self.pin,
                self.totp_secret,
            )
            if v and len(v) >= 4
        ]

    def __repr__(self) -> str:  # never leak via repr in a traceback
        return f"<Secrets client_id={'set' if self.client_id else 'unset'} token=***>"

    __str__ = __repr__
