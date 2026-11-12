"""FastAPI surface for the engine and the web app.

Security posture: binds to 127.0.0.1 by default (`--lan` opts into 0.0.0.0)
and requires the shared secret on every MUTATING call. Reads are open to
whoever can reach the port, which on the LAN means your own devices.

Every action endpoint is IDEMPOTENT: the state machine's compare-and-set is
the single arbiter, so a double-click, a retried request or two clients
racing produce exactly one position.

Nothing in this file can place, modify or cancel an order. The Dhan postback
endpoint only RECORDS what Dhan sends; it never acts on it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from ..broker.mock import MockBroker
from ..clock import IST

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class AckBody(BaseModel):
    action: str = Field(..., description="TAKE or SKIP")
    fill_price: float | None = Field(
        None, description="the price you ACTUALLY filled at, not the signalled price"
    )
    source: str = "dashboard"


class CloseBody(BaseModel):
    exit_price: float
    reason: str = "MANUAL"


class ReportBody(BaseModel):
    """The owner's own fill prices for a taken position (OF-5): kept beside the alert's."""

    entry_price: float | None = Field(None, description="the price you actually bought/sold at")
    exit_price: float | None = Field(None, description="the price you actually exited at")


class BacktestBody(BaseModel):
    strategy: str
    instrument_key: str
    timeframe: str = "5m"
    params: dict = Field(default_factory=dict)
    start: str | None = None
    end: str | None = None
    slippage_pct: float | None = None
    cost_multiplier: float = 1.0
    label: str = ""
    sentiment_mode: str = "off"  # off | follow | strict | contrarian
    sentiment_scope: str = "MARKET"
    sentiment_symbol: str | None = None
    sentiment_threshold: float = 0.2


class OptimizeBody(BacktestBody):
    kind: str = "optimize"  # optimize | walkforward
    trials: int = 40
    space: dict | None = None  # {param: {"choices":[...]} | {"lo","hi","step"}}
    is_days: int | None = None  # None = windows auto-sized from the available sessions
    oos_days: int | None = None


class DeskContextBody(BaseModel):
    key: str
    last: float
    change_pct: float | None = None
    note: str | None = None


class SweepBody(BaseModel):
    strategies: list[str] | None = None  # None = every registered strategy
    instruments: list[str] | None = None  # None = the desk watchlist (config/desk.yaml)
    timeframes: list[str] | None = None  # None = 5m, 15m, 30m, 60m
    presets: bool = False  # True = low / default / high setting per strategy (3x runs)
    slippage_pct: float | None = None
    cost_multiplier: float = 1.0
    sentiment_mode: str = "off"
    label: str = ""


class BackfillBody(BaseModel):
    series: list[str] | None = None
    resolutions: list[str] | None = None
    provider: str | None = None
    full: bool = False


class NewsBody(BaseModel):
    headline: str
    body: str | None = None
    source: str | None = None
    url: str | None = None
    ts: str | None = None
    symbols: list[str] = Field(default_factory=list)
    sentiment: float | None = Field(None, ge=-1.0, le=1.0)
    meta: dict = Field(default_factory=dict)


class SentimentBody(BaseModel):
    scope: str = Field(..., description="MARKET | INDEX | STOCK | SECTOR")
    symbol: str | None = None
    score: float = Field(..., ge=-1.0, le=1.0)
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    horizon: str | None = None
    source: str | None = None
    note: str | None = None
    ts: str | None = None


class SentimentImportBody(BaseModel):
    text: str = Field(..., description="CSV rows: date[,score[,confidence]]; header optional")
    scope: str = "MARKET"
    symbol: str | None = None
    source: str = "import"
    horizon: str | None = "daily"
    replace_source: bool = Field(
        False, description="Delete earlier rows from the same source first"
    )


class CredentialsBody(BaseModel):
    client_id: str | None = None
    access_token: str | None = None
    data_access_token: str | None = None
    pin: str | None = None
    totp_secret: str | None = None


def _iso(v):
    if isinstance(v, dt.datetime):
        return v.isoformat()
    return v


def _paper_payload(desk: object, now: dt.datetime) -> dict[str, Any]:
    """GET /api/paper (US-030, US-032) for `desk`'s book:
    {enabled, status, trades, totals_today, last_session}.

    `status` is the book's counters plus the desk's own `desk_hook_errors` (as in the desk
    snapshot's paper block), every key present. `last_session` is {date (IST, YYYY-MM-DD),
    totals (the shape of totals_today)} for the most recent earlier day with a closed or
    no-fill trade, or null. Without a desk or a book, or with paper.enabled false, it is
    `enabled: false` with no trades and null totals and last session.
    """
    from ..paper.book import RECENT_LIMIT, STATUS_KEYS
    from ..paper.view import last_session_view, totals_view

    book = getattr(desk, "paper", None) if desk is not None else None
    hook_errors = getattr(desk, "hook_errors", 0)
    status: dict[str, int] = dict.fromkeys(STATUS_KEYS, 0)
    if book is not None:
        status.update(book.status())
    # the desk's count, never the book's: a bool or a mock is not a count
    valid = isinstance(hook_errors, int) and not isinstance(hook_errors, bool)
    status["desk_hook_errors"] = hook_errors if valid else 0
    if book is None or not book.enabled:
        return {
            "enabled": False,
            "status": status,
            "trades": [],
            "totals_today": None,
            "last_session": None,
        }
    session = book.last_session(now)
    return {
        "enabled": True,
        "status": status,
        "trades": book.recent(RECENT_LIMIT),
        "totals_today": totals_view(book.today_totals(now)),
        "last_session": (
            None if session is None else last_session_view(session, book.day_totals(session))
        ),
    }


def _paper_scoreboard(desk: object) -> list[dict[str, Any]] | None:
    """The Home strategy scoreboard (OF-5) from the desk's paper book: every closed paper
    trade by slot (strategy, timeframe, market), the desk's primary slots first, so a
    strategy's trades from an earlier timeframe never count toward the paper test
    (2026-11-08). None without a book."""
    from collections import Counter

    from sqlalchemy.exc import SQLAlchemyError

    from ..paper.ledger import PaperLedger, Slot
    from ..paper.view import slot_scoreboard_view

    book = getattr(desk, "paper", None)
    delivery = getattr(desk, "delivery", None)
    if book is None and delivery is None:
        return None
    try:
        totals = PaperLedger(book.engine).by_slot() if book is not None else {}
        opened = Counter(
            Slot(str(t.strategy), str(t.timeframe), str(t.kind))
            for t in (book.open_trades() if book is not None else [])
        )
        if delivery is not None:  # US-067: the delivery pullback trades, a slot of their own
            try:
                for slot, row in delivery.book.by_slot().items():
                    totals[slot] = row
                opened[Slot(delivery.book.strategy, delivery.book.timeframe, "STOCK")] = (
                    delivery.book.open_count()
                )
            except Exception as exc:  # noqa: BLE001 - the intraday rows still show
                log.warning("the delivery scoreboard row could not be read: %s", type(exc).__name__)
    except (SQLAlchemyError, OSError) as exc:
        log.warning("the paper scoreboard could not be read: %s", type(exc).__name__)
        return None
    primary = desk.cfg.primary_slots() if hasattr(desk, "cfg") else ()
    paused = desk.cfg.paused_slots() if hasattr(desk, "cfg") else frozenset()
    return slot_scoreboard_view(totals, primary, opened, paused)


def create_app(engine, api_token: str) -> FastAPI:
    app = FastAPI(
        title="One Lot Bot — Dhan trade desk",
        description="Signal-only. This service places no orders.",
        version="3.0.0",
        docs_url="/api/docs",
    )
    db = engine.db

    def require_token(request: Request) -> None:
        """Reads are open; MUTATIONS always require the secret."""
        supplied = request.headers.get("x-onelot-token") or request.query_params.get("token", "")
        if not api_token or supplied != api_token:
            raise HTTPException(
                status_code=401,
                detail=(
                    "invalid or missing API token: this page's session token no longer matches the engine "
                    "(the engine was restarted or data/api_token.txt changed). Reload the page once."
                ),
            )

    # ── live desk (unchanged contract) ────────────────────────────────────
    @app.get("/api/state")
    def state() -> dict:
        return engine.snapshot()

    @app.get("/api/health")
    def health() -> dict:
        snap = engine.snapshot()
        return {
            "ok": bool(snap.get("health")),
            "health": snap.get("health"),
            "status": snap.get("status"),
        }

    @app.get("/api/notifications")
    def notifications(since: int = Query(0, ge=0)) -> dict:
        items, seq = engine.notifier.dashboard.since(since)
        return {"items": items, "sequence": seq}

    @app.get("/api/gates")
    def gates(hours: int = Query(24, ge=1, le=720)) -> dict:
        return {"hours": hours, "histogram": engine.gate_histogram(hours)}

    @app.get("/api/journal")
    def journal() -> dict:
        return {
            "metrics": engine.journal.metrics(engine.cfg.mode),
            "trades": engine.journal.entries(engine.cfg.mode, limit=200),
        }

    @app.get("/api/journal.csv", response_class=PlainTextResponse)
    def journal_csv() -> str:
        return engine.journal.to_csv(engine.cfg.mode)

    @app.get("/api/events")
    def events(limit: int = Query(100, ge=1, le=1000)) -> dict:
        return {"events": engine.repo.recent_events(limit)}

    @app.get("/api/stream")
    async def stream():
        async def gen():
            last_seq = 0
            last_desk_version = -1
            while True:
                try:
                    snap = engine.snapshot()
                    # The desk snapshot is the bulk of the payload and changes once per poll:
                    # ship it only when its version moved (the client keeps the last copy).
                    d = snap.get("desk")
                    if isinstance(d, dict):
                        v = d.get("version", 0)
                        if v == last_desk_version:
                            snap = {**snap, "desk": {"version": v, "unchanged": True}}
                        else:
                            last_desk_version = v
                    items, seq = engine.notifier.dashboard.since(last_seq)
                    last_seq = seq
                    yield f"data: {json.dumps({'state': snap, 'notifications': items}, default=str)}\n\n"
                except Exception:
                    log.exception("SSE generation failed")
                    yield 'data: {"error": "snapshot failed"}\n\n'
                await asyncio.sleep(1.0)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/signal/{signal_id}/ack", dependencies=[Depends(require_token)])
    def ack(signal_id: str, body: AckBody) -> dict:
        if body.action.upper() not in ("TAKE", "TAKEN", "SKIP", "SKIPPED"):
            raise HTTPException(400, "action must be TAKE or SKIP")
        return engine.acknowledge(signal_id, body.action, body.source, body.fill_price)

    @app.post("/api/position/{position_id}/close", dependencies=[Depends(require_token)])
    def close(position_id: str, body: CloseBody) -> dict:
        result = engine.close_position(position_id, body.exit_price, body.reason)
        if result is None:
            raise HTTPException(404, "position not found or already closed")
        return result

    @app.post("/api/risk/halt", dependencies=[Depends(require_token)])
    def halt() -> dict:
        return engine.panic_halt()

    @app.post("/api/risk/reset", dependencies=[Depends(require_token)])
    def reset() -> dict:
        return engine.reset_halt()

    @app.post("/api/notify/test", dependencies=[Depends(require_token)])
    def notify_test() -> dict:
        return engine.notifier.self_test()

    # ── Live Desk (multi-instrument; alerts only) ─────────────────────────
    def _desk():
        if engine.desk is None:
            raise HTTPException(503, "Live Desk not running")
        return engine.desk

    @app.get("/api/desk")
    def desk_state() -> dict:
        return _desk().snapshot()

    @app.post("/api/desk/signal/{signal_id}/ack", dependencies=[Depends(require_token)])
    def desk_ack(signal_id: str, body: AckBody) -> dict:
        return _desk().acknowledge(signal_id, body.action, body.fill_price, body.source)

    @app.post("/api/desk/position/{position_id}/close", dependencies=[Depends(require_token)])
    def desk_close(position_id: str, body: CloseBody) -> dict:
        res = _desk().close_position(position_id, body.exit_price, body.reason)
        if res is None:
            raise HTTPException(404, "position not found or already closed")
        return res

    @app.post("/api/desk/position/{position_id}/report", dependencies=[Depends(require_token)])
    def desk_report(position_id: str, body: ReportBody) -> dict:
        """Your own entry and/or exit price for a taken position, open or closed (OF-5)."""
        res = _desk().report_prices(position_id, body.entry_price, body.exit_price)
        if res is None:
            raise HTTPException(404, "position not found")
        return res

    @app.post("/api/desk/context", dependencies=[Depends(require_token)])
    def desk_context(body: DeskContextBody) -> dict:
        """Feed a context value the desk has no free source for (GIFT NIFTY)."""
        return _desk().set_context(body.key, body.last, body.change_pct, body.note)

    @app.post("/api/desk/reload", dependencies=[Depends(require_token)])
    def desk_reload() -> dict:
        return _desk().reload()

    @app.post("/api/desk/poll", dependencies=[Depends(require_token)])
    def desk_poll() -> dict:
        d = _desk()
        d._wake.set()
        return {"ok": True}

    @app.get("/api/desk/fill-costs")
    def desk_fill_costs(days: int = Query(30, ge=1, le=365)) -> dict:
        """Measured one-unit fill costs (bid/ask quotes, never an order) beside the Lab's
        slippage assumptions: spreads per instrument and time of day, every alert's spread
        and the drift 1, 2 and 5 minutes later, and the owner's own fills."""
        from ..desk.fillcost import report

        d = _desk()
        out = report(d.engine, days)
        try:  # the owner's own fills: Dhan's billed charges against the cost table
            out["charges"] = d.fill_charges()
        except Exception as exc:  # noqa: BLE001 - quotes still answer without the trade book
            out["charges"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return out

    @app.get("/api/desk/journal")
    def desk_journal(limit: int = Query(200, ge=1, le=2000)) -> dict:
        return {"trades": _desk().journal(limit)}

    @app.get("/api/desk/signals")
    def desk_signals_view(days: int = Query(7, ge=1, le=60)) -> dict:
        """Every alert of the last `days` with its audited outcome and its exit (OF-5), the
        audit scoreboard, and the paper scoreboard of the primary strategies."""
        d = _desk()
        out = d.signals_view(days)
        out["primary_strategies"] = list(d.cfg.primary_strategies())
        out["paper_scoreboard"] = _paper_scoreboard(d)
        return out

    @app.get("/api/desk/bars")
    def desk_bars(
        key: str, timeframe: str | None = None, n: int = Query(300, ge=20, le=3000)
    ) -> dict:
        return _desk().bars_view(key, timeframe, n)

    @app.get("/api/paper")
    def paper_view() -> dict:
        """The paper book for the desk's Paper panel (US-030). Read-only.

        Reads the database through the book; never the Desk lock. No desk, no book, or
        paper.enabled false: enabled false, no trades, no totals (the counts still shown).
        """
        from sqlalchemy.exc import SQLAlchemyError

        try:
            return _paper_payload(engine.desk, dt.datetime.now(IST))
        except (SQLAlchemyError, OSError) as exc:
            log.warning("GET /api/paper: the paper book could not be read: %s", exc)
            # One line for the panel: the driver's message, never the SQL or its parameters.
            text = str(getattr(exc, "orig", None) or exc).strip() or type(exc).__name__
            brief = text.splitlines()[0]
            raise HTTPException(503, f"paper book unreadable: {brief}") from exc

    # ── account (free Trading API; cached, never polled) ──────────────────
    @app.get("/api/account")
    def account(refresh: int = Query(0, ge=0, le=1)) -> dict:
        return engine.account.snapshot(refresh=bool(refresh))

    @app.post("/api/account/refresh", dependencies=[Depends(require_token)])
    def account_refresh() -> dict:
        res = engine.account.refresh()
        return {"refreshed": res, "snapshot": engine.account.snapshot()}

    # ── Backtest Lab ──────────────────────────────────────────────────────
    @app.get("/api/lab/strategies")
    def lab_strategies() -> dict:
        from ..strategies.registry import list_strategies

        return {"strategies": list_strategies()}

    @app.get("/api/lab/instruments")
    def lab_instruments() -> dict:
        from ..data.store import coverage_by_series
        from ..data.universe import load_specs, tradeable_instruments

        series, instruments = load_specs()
        cov = coverage_by_series(db.engine)
        out = []
        for inst in instruments.values():
            d = inst.as_dict()
            d["coverage"] = cov.get(inst.series_key, {})
            s = series.get(inst.series_key)
            d["has_volume"] = bool(s.has_volume) if s else True
            out.append(d)
        order = {"OPTIONS": 0, "FUTURES": 1, "STOCK": 2, "CURRENCY": 3, "INDEX": 4}
        out.sort(key=lambda d: (order.get(d["category"], 9), d["key"]))
        return {
            "instruments": out,
            "tradeable": [i.key for i in tradeable_instruments(instruments)],
        }

    @app.get("/api/lab/coverage")
    def lab_coverage() -> dict:
        """The bars stored per series, resolution and source (`coverage`), and what the Lab
        can load per timeframe after deriving from finer bars (`effective`, OF-3), with the
        history each provider serves (`limits`)."""
        from ..data.store import SOURCE_LIMITS, coverage_report, effective_coverage

        cov = coverage_report(db.engine)
        rows = cov.to_dict("records") if not cov.empty else []
        return {"coverage": rows, "effective": effective_coverage(rows), "limits": SOURCE_LIMITS}

    @app.post("/api/lab/backtest", dependencies=[Depends(require_token)])
    def lab_backtest(body: BacktestBody) -> dict:
        from ..lab.runner import BacktestConfig

        cfg = BacktestConfig.from_settings(
            body.strategy,
            body.instrument_key.upper(),
            params=body.params,
            timeframe=body.timeframe,
            start=body.start,
            end=body.end,
            cost_multiplier=body.cost_multiplier,
            label=body.label,
            sentiment_mode=body.sentiment_mode,
            sentiment_scope=body.sentiment_scope,
            sentiment_symbol=body.sentiment_symbol,
            sentiment_threshold=body.sentiment_threshold,
            **({"slippage_pct": body.slippage_pct} if body.slippage_pct is not None else {}),
        )
        rid = engine.lab_jobs.submit_backtest(
            cfg.__dict__,
            label=body.label or f"{body.strategy} {body.instrument_key} {body.timeframe}",
        )
        return {"run_id": rid}

    @app.post("/api/lab/optimize", dependencies=[Depends(require_token)])
    def lab_optimize(body: OptimizeBody) -> dict:
        from ..lab.research import SPACES
        from ..lab.runner import BacktestConfig
        from ..strategies.registry import REGISTRY

        cfg = BacktestConfig.from_settings(
            body.strategy,
            body.instrument_key.upper(),
            params=body.params,
            timeframe=body.timeframe,
            start=body.start,
            end=body.end,
            label=body.label,
        )
        space = body.space
        if not space:
            space = SPACES.get(body.strategy)
        if not space:
            cls = REGISTRY.get(body.strategy)
            if cls is None:
                raise HTTPException(404, f"unknown strategy {body.strategy}")
            space = {
                k: {"lo": lo, "hi": hi, "step": st} for k, (lo, hi, st) in cls.param_space().items()
            }
        rid = engine.lab_jobs.submit_optimize(
            cfg.__dict__,
            space,
            trials=max(5, min(body.trials, 300)),
            kind=body.kind,
            wf={"is_days": body.is_days, "oos_days": body.oos_days},
            label=body.label
            or f"{body.kind} {body.strategy} {body.instrument_key} {body.timeframe}",
        )
        return {"run_id": rid}

    @app.get("/api/lab/runs")
    def lab_runs(limit: int = Query(50, ge=1, le=500)) -> dict:
        return {"runs": engine.lab_jobs.list_runs(limit)}

    @app.post("/api/lab/runs/{rid}/stop", dependencies=[Depends(require_token)])
    def lab_run_stop(rid: int) -> dict:
        return {"stopped": engine.lab_jobs.stop_run(rid)}

    @app.post("/api/lab/sweep", dependencies=[Depends(require_token)])
    def lab_sweep(body: SweepBody) -> dict:
        from ..lab.sweep import plan

        try:
            combos = plan(body.strategies, body.instruments, body.timeframes, presets=body.presets)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if len(combos) > 4000:
            raise HTTPException(
                400, f"{len(combos)} runs is too many for one sweep; narrow the grid"
            )
        base = {"cost_multiplier": body.cost_multiplier, "sentiment_mode": body.sentiment_mode}
        if body.slippage_pct is not None:
            base["slippage_pct"] = body.slippage_pct
        rid = engine.lab_jobs.submit_sweep(combos, label=body.label, base=base)
        return {"run_id": rid, "combos": len(combos)}

    @app.get("/api/lab/leaderboard")
    def lab_leaderboard(
        strategy: str | None = None,
        instrument: str | None = None,
        timeframe: str | None = None,
        category: str | None = None,
        kind: str | None = None,
        sweep_id: int | None = None,
        min_trades: int = Query(10, ge=1, le=500),
        limit: int = Query(300, ge=1, le=2000),
        sort: str = Query(
            "score",
            pattern="^(score|win_rate|profit_pct|profit_factor|expectancy_r|net_pnl|trades)$",
        ),
        order: str = Query("desc", pattern="^(asc|desc)$"),
        strategies: str | None = Query(None, max_length=400),
    ) -> dict:
        from ..lab.sweep import leaderboard, summarise

        rows = leaderboard(
            db.engine,
            strategy=strategy,
            instrument=instrument,
            timeframe=timeframe,
            category=category,
            kind=kind,
            min_trades=min_trades,
            sweep_id=sweep_id,
            limit=100000 if strategies else limit,
            sort=sort,
            order=order,
        )
        if strategies:  # e.g. the desk's primary strategies, comma separated
            keep = {x.strip() for x in strategies.split(",") if x.strip()}
            rows = [r for r in rows if r["strategy"] in keep][:limit]
        return {"rows": rows, "summary": summarise(rows), "sort": sort, "order": order}

    @app.get("/api/lab/matrix")
    def lab_matrix(sweep_id: int | None = None, min_trades: int = Query(5, ge=1, le=500)) -> dict:
        from ..lab.sweep import matrix

        return matrix(db.engine, sweep_id=sweep_id, min_trades=min_trades)

    @app.get("/api/lab/presets")
    def lab_presets() -> dict:
        from ..lab.presets import all_presets

        return {"presets": all_presets()}

    @app.get("/api/lab/focus")
    def lab_focus() -> dict:
        from ..desk.config import load_desk_config
        from ..lab.sweep import FOCUS_TIMEFRAMES, focus_instruments

        desk = getattr(engine, "desk", None)
        cfg = getattr(desk, "cfg", None) or load_desk_config()
        return {
            "instruments": focus_instruments(),
            "timeframes": FOCUS_TIMEFRAMES,
            "primary_strategies": list(cfg.primary_strategies()),
        }

    @app.get("/api/lab/runs/{rid}")
    def lab_run(rid: int, trades: int = Query(1, ge=0, le=1)) -> dict:
        out = engine.lab_jobs.get_run(rid, with_trades=bool(trades))
        if out is None:
            raise HTTPException(404, "run not found")
        return out

    @app.get("/api/lab/param_sets")
    def lab_param_sets(strategy: str | None = None) -> dict:
        from ..data.schema import param_sets

        q = select(param_sets).order_by(desc(param_sets.c.created_at))
        if strategy:
            q = q.where(param_sets.c.strategy == strategy)
        with db.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(q.limit(200)).mappings().all()]
        for r in rows:
            r["created_at"] = _iso(r.get("created_at"))
        return {"param_sets": rows}

    @app.get("/api/lab/research_report", response_class=PlainTextResponse)
    def lab_research_report() -> str:
        p = engine.cfg.project_root / "docs" / "research" / "RESEARCH_REPORT.md"
        return (
            p.read_text(encoding="utf-8")
            if p.exists()
            else "No research report yet. Run `python -m onelot research`."
        )

    # ── data corpus ───────────────────────────────────────────────────────
    @app.post("/api/data/backfill", dependencies=[Depends(require_token)])
    def data_backfill(body: BackfillBody) -> dict:
        adapter = getattr(engine, "account_adapter", None)
        jid = engine.lab_jobs.submit_backfill(
            body.series, body.resolutions, body.provider, adapter, body.full
        )
        return {"job_id": jid}

    @app.get("/api/data/jobs")
    def data_jobs(limit: int = Query(20, ge=1, le=200)) -> dict:
        return {"jobs": engine.lab_jobs.list_jobs(limit)}

    @app.post("/api/data/jobs/{jid}/stop", dependencies=[Depends(require_token)])
    def data_job_stop(jid: int) -> dict:
        return {"stopped": engine.lab_jobs.stop_job(jid)}

    @app.get("/api/data/quality")
    def data_quality(resolution: str = Query("5m")) -> dict:
        from ..data.backfill import quality_report

        q = quality_report(db.engine, resolution)
        return {
            "resolution": resolution,
            "rows": json.loads(q.to_json(orient="records")) if not q.empty else [],
        }

    # ── news + sentiment (endpoints now; the scorer is a separate project) ─
    @app.get("/api/data/eda")
    def data_eda(series: str = Query("BANKNIFTY"), resolution: str = Query("5m")) -> dict:
        from ..data.eda import series_eda

        return series_eda(db.engine, series.upper(), resolution)

    @app.get("/api/data/correlation")
    def data_correlation(series: str | None = None) -> dict:
        from ..data.eda import correlation
        from ..lab.sweep import desk_config

        if series:
            keys = [k.strip().upper() for k in series.split(",") if k.strip()]
        else:
            cfg_d = desk_config()
            keys = ["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"] + [
                str(x["key"]).upper() for x in cfg_d.get("stocks") or []
            ]
        return correlation(db.engine, keys)

    @app.get("/api/news")
    def news(limit: int = Query(100, ge=1, le=1000), symbol: str | None = None) -> dict:
        from ..data.schema import news_items

        q = select(news_items).order_by(desc(news_items.c.ts)).limit(limit)
        with db.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(q).mappings().all()]
        if symbol:
            rows = [
                r
                for r in rows
                if symbol.upper() in [str(s).upper() for s in (r.get("symbols") or [])]
            ]
        for r in rows:
            r["ts"], r["created_at"] = _iso(r.get("ts")), _iso(r.get("created_at"))
        return {"items": rows}

    @app.post("/api/news", dependencies=[Depends(require_token)])
    def news_add(body: NewsBody) -> dict:
        from ..data.db import utcnow
        from ..data.schema import news_items

        ts = dt.datetime.fromisoformat(body.ts) if body.ts else utcnow()
        with db.engine.begin() as conn:
            nid = conn.execute(
                news_items.insert().values(
                    ts=ts,
                    source=body.source,
                    headline=body.headline,
                    body=body.body,
                    url=body.url,
                    symbols=[s.upper() for s in body.symbols],
                    sentiment=body.sentiment,
                    meta=body.meta,
                    created_at=utcnow(),
                )
            ).inserted_primary_key[0]
        return {"id": int(nid)}

    @app.get("/api/sentiment")
    def sentiment(
        limit: int = Query(200, ge=1, le=2000), scope: str | None = None, symbol: str | None = None
    ) -> dict:
        from ..data.schema import sentiment_scores

        q = select(sentiment_scores).order_by(desc(sentiment_scores.c.ts)).limit(limit)
        if scope:
            q = q.where(sentiment_scores.c.scope == scope.upper())
        if symbol:
            q = q.where(sentiment_scores.c.symbol == symbol.upper())
        with db.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(q).mappings().all()]
        for r in rows:
            r["ts"], r["created_at"] = _iso(r.get("ts")), _iso(r.get("created_at"))
        return {"scores": rows}

    @app.get("/api/sentiment/latest")
    def sentiment_latest() -> dict:
        from ..data.schema import sentiment_scores

        q = select(sentiment_scores).order_by(desc(sentiment_scores.c.ts)).limit(2000)
        latest: dict[str, dict] = {}
        with db.engine.connect() as conn:
            for r in conn.execute(q).mappings().all():
                key = f"{r['scope']}:{r['symbol'] or '*'}"
                if key not in latest:
                    d = dict(r)
                    d["ts"], d["created_at"] = _iso(d.get("ts")), _iso(d.get("created_at"))
                    latest[key] = d
        return {"latest": latest}

    @app.post("/api/sentiment", dependencies=[Depends(require_token)])
    def sentiment_add(body: SentimentBody) -> dict:
        from ..data.db import utcnow
        from ..data.schema import sentiment_scores

        ts = dt.datetime.fromisoformat(body.ts) if body.ts else utcnow()
        with db.engine.begin() as conn:
            sid = conn.execute(
                sentiment_scores.insert().values(
                    ts=ts,
                    scope=body.scope.upper(),
                    symbol=(body.symbol or "").upper() or None,
                    score=body.score,
                    confidence=body.confidence,
                    horizon=body.horizon,
                    source=body.source,
                    note=body.note,
                    created_at=utcnow(),
                )
            ).inserted_primary_key[0]
        return {"id": int(sid)}

    @app.get("/api/sentiment/pulse")
    def sentiment_pulse(refresh: int = Query(0, ge=0, le=1)) -> dict:
        d = engine.desk
        if d is None:
            return {"score": None, "label": "desk not running", "components": {}}
        return d.pulse.compute() if refresh or d.pulse.last.get("score") is None else d.pulse.last

    @app.post("/api/sentiment/import", dependencies=[Depends(require_token)])
    def sentiment_import(body: SentimentImportBody) -> dict:
        """Bulk-load historical scores (your scorer's output) for backtests."""
        from sqlalchemy import delete

        from ..data.db import utcnow
        from ..data.schema import sentiment_scores
        from ..lab.sentiment import parse_import

        rows = parse_import(body.text)
        if not rows:
            raise HTTPException(400, "no rows parsed; expected lines like 2024-01-02,0.62")
        scope, symbol = body.scope.upper(), (body.symbol or "").upper() or None
        with db.engine.begin() as conn:
            if body.replace_source:
                conn.execute(
                    delete(sentiment_scores).where(
                        (sentiment_scores.c.source == body.source)
                        & (sentiment_scores.c.scope == scope)
                        & (
                            (sentiment_scores.c.symbol == symbol)
                            if symbol
                            else sentiment_scores.c.symbol.is_(None)
                        )
                    )
                )
            conn.execute(
                sentiment_scores.insert(),
                [
                    {
                        "ts": r["ts"],
                        "scope": scope,
                        "symbol": symbol,
                        "score": r["score"],
                        "confidence": r["confidence"],
                        "horizon": body.horizon,
                        "source": body.source,
                        "note": None,
                        "created_at": utcnow(),
                    }
                    for r in rows
                ],
            )
        return {
            "imported": len(rows),
            "first": rows[0]["ts"].isoformat(),
            "last": rows[-1]["ts"].isoformat(),
            "scope": scope,
            "symbol": symbol,
            "source": body.source,
        }

    # ── settings / credentials ────────────────────────────────────────────
    @app.get("/api/settings")
    def settings() -> dict:
        cfg = engine.cfg
        return {
            "credentials": engine.secrets.masked(),
            "account_adapter": engine.account.adapter is not None,
            "feed_broker": getattr(engine.broker, "name", None),
            "mode": cfg.mode,
            "api": {"host": cfg.api.host, "port": cfg.api.port},
            "backtest": cfg.backtest.model_dump(),
            "live": cfg.strategy.live.model_dump(),
            "risk": cfg.risk.model_dump(),
            "data": {
                "provider": cfg.data.provider,
                "history_start": cfg.data.history_start,
                "corpus_resolutions": cfg.data.corpus_resolutions,
            },
            "postback_url_hint": f"http://{cfg.api.host}:{cfg.api.port}/api/dhan/postback",
            "env_path": str(cfg.project_root / ".env"),
            "feed_live": not isinstance(engine.broker, MockBroker),
            "data_api_active": engine._data_api_active(),
            "token_expiry": _iso(engine.token_expiry()),
            "can_self_renew": engine.secrets.can_self_renew,
            "last_renew_at": _iso(getattr(engine, "_last_renew_at", None)),
            "last_renew_error": getattr(engine, "_last_renew_error", None),
            "desk_source": (engine.desk.snapshot().get("source") if engine.desk else None),
        }

    @app.post("/api/settings/dhan", dependencies=[Depends(require_token)])
    def settings_dhan(body: CredentialsBody) -> dict:
        if not any(
            [body.client_id, body.access_token, body.data_access_token, body.pin, body.totp_secret]
        ):
            raise HTTPException(400, "nothing to update")
        return engine.update_credentials(
            body.client_id, body.access_token, body.data_access_token, body.pin, body.totp_secret
        )

    @app.post("/api/settings/dhan/renew", dependencies=[Depends(require_token)])
    def settings_dhan_renew() -> dict:
        """Mint a fresh 24-hour token from the stored PIN + TOTP secret."""
        return engine.renew_token(force=True)

    @app.post("/api/settings/dhan/golive", dependencies=[Depends(require_token)])
    def settings_dhan_golive() -> dict:
        engine.account.refresh(("profile",))
        return engine.go_live()

    @app.get("/api/session")
    def session_token_probe() -> dict:
        """Lets a stale page re-sync its session token without a reload."""
        return {"token": api_token}

    @app.get("/api/calendar")
    def calendar(year: int | None = None) -> dict:
        import yaml

        path = engine.cfg.project_root / "config" / "nse_holidays.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        today = dt.datetime.now(IST).date()
        y = year or today.year

        def rows(section):
            out = []
            for e in (raw.get(section) or {}).get(y) or []:
                d = dt.date.fromisoformat(str(e["date"]))
                out.append(
                    {
                        "date": d.isoformat(),
                        "weekday": d.strftime("%a"),
                        "name": str(e.get("name", "")),
                        "past": d < today,
                        "today": d == today,
                        "days_away": (d - today).days,
                    }
                )
            return out

        hol = rows("holidays")
        upcoming = [h for h in hol if not h["past"]]
        window = engine.session_clock.current_window(dt.datetime.now(IST))
        return {
            "year": y,
            "verified": bool(raw.get("verified")),
            "verified_on": raw.get("verified_on"),
            "sources": raw.get("sources") or [],
            "holidays": hol,
            "weekend_holidays": rows("weekend_holidays"),
            "special_sessions": [
                {**x, "date": str(x.get("date"))} for x in (raw.get("special_sessions") or [])
            ],
            "next_holiday": upcoming[0] if upcoming else None,
            "today": today.isoformat(),
            "today_is_trading_day": window is not None,
            "session": {"open": "09:15", "close": "15:30", "square_off": "15:15"},
            "years_available": sorted(int(k) for k in (raw.get("holidays") or {})),
        }

    @app.post("/api/settings/dhan/test", dependencies=[Depends(require_token)])
    def settings_dhan_test() -> dict:
        adapter = engine.account.adapter
        if adapter is None:
            raise HTTPException(400, "no Dhan credentials configured")
        res = adapter.preflight(
            engine.cfg.instrument.index_security_id, engine.cfg.instrument.exchange_segment_index
        )
        return res

    @app.post("/api/dhan/postback")
    async def dhan_postback(request: Request) -> dict:
        """Dhan order-update webhook target. RECORDED ONLY — never acted upon.
        (We place no orders, so nothing should ever arrive here.)"""
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {"raw": (await request.body())[:2000].decode("utf-8", "replace")}
        engine.repo.event(
            dt.datetime.now(dt.timezone.utc),
            "INFO",
            "dhan_postback",
            "postback received (recorded, not acted upon)",
            payload,
        )
        return {"received": True}

    # ── UI ────────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        return html.replace("__API_TOKEN__", api_token)

    @app.get("/static/{name:path}")
    def static(name: str):
        p = (UI_DIR / name).resolve()
        if not p.is_file() or UI_DIR.resolve() not in p.parents:
            raise HTTPException(404, "not found")
        media = {
            "js": "application/javascript",
            "css": "text/css",
            "svg": "image/svg+xml",
            "png": "image/png",
            "woff2": "font/woff2",
            "woff": "font/woff",
            "ttf": "font/ttf",
            "json": "application/json",
        }.get(p.suffix.lstrip("."), "application/octet-stream")
        # Vendored libraries and fonts are immutable per version: cache them hard.
        cache = "public, max-age=604800" if name.startswith("vendor/") else "no-cache"
        return FileResponse(str(p), media_type=media, headers={"Cache-Control": cache})

    return app
