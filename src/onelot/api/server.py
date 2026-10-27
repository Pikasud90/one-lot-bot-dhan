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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class AckBody(BaseModel):
    action: str = Field(..., description="TAKE or SKIP")
    fill_price: float | None = Field(
        None, description="the price you ACTUALLY filled at, not the signalled price")
    source: str = "dashboard"


class CloseBody(BaseModel):
    exit_price: float
    reason: str = "MANUAL"


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


class OptimizeBody(BacktestBody):
    kind: str = "optimize"          # optimize | walkforward
    trials: int = 40
    space: dict | None = None       # {param: {"choices":[...]} | {"lo","hi","step"}}
    is_days: int = 40
    oos_days: int = 10


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


class CredentialsBody(BaseModel):
    client_id: str | None = None
    access_token: str | None = None
    data_access_token: str | None = None


def _iso(v):
    if isinstance(v, dt.datetime):
        return v.isoformat()
    return v


def create_app(engine, api_token: str) -> FastAPI:
    app = FastAPI(title="One Lot Bot — Dhan trade desk",
                  description="Signal-only. This service places no orders.",
                  version="3.0.0", docs_url="/api/docs")
    db = engine.db

    def require_token(request: Request) -> None:
        """Reads are open; MUTATIONS always require the secret."""
        supplied = (request.headers.get("x-onelot-token")
                    or request.query_params.get("token", ""))
        if not api_token or supplied != api_token:
            raise HTTPException(status_code=401, detail="invalid or missing API token")

    # ── live desk (unchanged contract) ────────────────────────────────────
    @app.get("/api/state")
    def state() -> dict:
        return engine.snapshot()

    @app.get("/api/health")
    def health() -> dict:
        snap = engine.snapshot()
        return {"ok": bool(snap.get("health")), "health": snap.get("health"), "status": snap.get("status")}

    @app.get("/api/notifications")
    def notifications(since: int = Query(0, ge=0)) -> dict:
        items, seq = engine.notifier.dashboard.since(since)
        return {"items": items, "sequence": seq}

    @app.get("/api/gates")
    def gates(hours: int = Query(24, ge=1, le=720)) -> dict:
        return {"hours": hours, "histogram": engine.gate_histogram(hours)}

    @app.get("/api/journal")
    def journal() -> dict:
        return {"metrics": engine.journal.metrics(engine.cfg.mode),
                "trades": engine.journal.entries(engine.cfg.mode, limit=200)}

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
            while True:
                try:
                    snap = engine.snapshot()
                    items, seq = engine.notifier.dashboard.since(last_seq)
                    last_seq = seq
                    yield f"data: {json.dumps({'state': snap, 'notifications': items}, default=str)}\n\n"
                except Exception:
                    log.exception("SSE generation failed")
                    yield 'data: {"error": "snapshot failed"}\n\n'
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
        return {"instruments": out, "tradeable": [i.key for i in tradeable_instruments(instruments)]}

    @app.get("/api/lab/coverage")
    def lab_coverage() -> dict:
        from ..data.store import coverage_report

        cov = coverage_report(db.engine)
        return {"coverage": cov.to_dict("records") if not cov.empty else []}

    @app.post("/api/lab/backtest", dependencies=[Depends(require_token)])
    def lab_backtest(body: BacktestBody) -> dict:
        from ..lab.runner import BacktestConfig

        cfg = BacktestConfig.from_settings(
            body.strategy, body.instrument_key.upper(), params=body.params, timeframe=body.timeframe,
            start=body.start, end=body.end, cost_multiplier=body.cost_multiplier, label=body.label,
            **({"slippage_pct": body.slippage_pct} if body.slippage_pct is not None else {}))
        rid = engine.lab_jobs.submit_backtest(cfg.__dict__, label=body.label or f"{body.strategy} {body.instrument_key} {body.timeframe}")
        return {"run_id": rid}

    @app.post("/api/lab/optimize", dependencies=[Depends(require_token)])
    def lab_optimize(body: OptimizeBody) -> dict:
        from ..lab.research import SPACES
        from ..lab.runner import BacktestConfig
        from ..strategies.registry import REGISTRY

        cfg = BacktestConfig.from_settings(
            body.strategy, body.instrument_key.upper(), params=body.params, timeframe=body.timeframe,
            start=body.start, end=body.end, label=body.label)
        space = body.space
        if not space:
            space = SPACES.get(body.strategy)
        if not space:
            cls = REGISTRY.get(body.strategy)
            if cls is None:
                raise HTTPException(404, f"unknown strategy {body.strategy}")
            space = {k: {"lo": lo, "hi": hi, "step": st} for k, (lo, hi, st) in cls.param_space().items()}
        rid = engine.lab_jobs.submit_optimize(
            cfg.__dict__, space, trials=max(5, min(body.trials, 300)), kind=body.kind,
            wf={"is_days": body.is_days, "oos_days": body.oos_days},
            label=body.label or f"{body.kind} {body.strategy} {body.instrument_key} {body.timeframe}")
        return {"run_id": rid}

    @app.get("/api/lab/runs")
    def lab_runs(limit: int = Query(50, ge=1, le=500)) -> dict:
        return {"runs": engine.lab_jobs.list_runs(limit)}

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
        return p.read_text(encoding="utf-8") if p.exists() else "No research report yet. Run `python -m onelot research`."

    # ── data corpus ───────────────────────────────────────────────────────
    @app.post("/api/data/backfill", dependencies=[Depends(require_token)])
    def data_backfill(body: BackfillBody) -> dict:
        adapter = getattr(engine, "account_adapter", None)
        jid = engine.lab_jobs.submit_backfill(body.series, body.resolutions, body.provider, adapter, body.full)
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
        return {"resolution": resolution, "rows": json.loads(q.to_json(orient="records")) if not q.empty else []}

    # ── news + sentiment (endpoints now; the scorer is a separate project) ─
    @app.get("/api/news")
    def news(limit: int = Query(100, ge=1, le=1000), symbol: str | None = None) -> dict:
        from ..data.schema import news_items

        q = select(news_items).order_by(desc(news_items.c.ts)).limit(limit)
        with db.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(q).mappings().all()]
        if symbol:
            rows = [r for r in rows if symbol.upper() in [str(s).upper() for s in (r.get("symbols") or [])]]
        for r in rows:
            r["ts"], r["created_at"] = _iso(r.get("ts")), _iso(r.get("created_at"))
        return {"items": rows}

    @app.post("/api/news", dependencies=[Depends(require_token)])
    def news_add(body: NewsBody) -> dict:
        from ..data.db import utcnow
        from ..data.schema import news_items

        ts = dt.datetime.fromisoformat(body.ts) if body.ts else utcnow()
        with db.engine.begin() as conn:
            nid = conn.execute(news_items.insert().values(
                ts=ts, source=body.source, headline=body.headline, body=body.body, url=body.url,
                symbols=[s.upper() for s in body.symbols], sentiment=body.sentiment, meta=body.meta,
                created_at=utcnow())).inserted_primary_key[0]
        return {"id": int(nid)}

    @app.get("/api/sentiment")
    def sentiment(limit: int = Query(200, ge=1, le=2000), scope: str | None = None,
                  symbol: str | None = None) -> dict:
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
            sid = conn.execute(sentiment_scores.insert().values(
                ts=ts, scope=body.scope.upper(), symbol=(body.symbol or "").upper() or None,
                score=body.score, confidence=body.confidence, horizon=body.horizon, source=body.source,
                note=body.note, created_at=utcnow())).inserted_primary_key[0]
        return {"id": int(sid)}

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
            "data": {"provider": cfg.data.provider, "history_start": cfg.data.history_start,
                     "corpus_resolutions": cfg.data.corpus_resolutions},
            "postback_url_hint": f"http://{cfg.api.host}:{cfg.api.port}/api/dhan/postback",
            "env_path": str(cfg.project_root / ".env"),
        }

    @app.post("/api/settings/dhan", dependencies=[Depends(require_token)])
    def settings_dhan(body: CredentialsBody) -> dict:
        if not any([body.client_id, body.access_token, body.data_access_token]):
            raise HTTPException(400, "nothing to update")
        return engine.update_credentials(body.client_id, body.access_token, body.data_access_token)

    @app.post("/api/settings/dhan/test", dependencies=[Depends(require_token)])
    def settings_dhan_test() -> dict:
        adapter = engine.account.adapter
        if adapter is None:
            raise HTTPException(400, "no Dhan credentials configured")
        res = adapter.preflight(engine.cfg.instrument.index_security_id,
                                engine.cfg.instrument.exchange_segment_index)
        return res

    @app.post("/api/dhan/postback")
    async def dhan_postback(request: Request) -> dict:
        """Dhan order-update webhook target. RECORDED ONLY — never acted upon.
        (We place no orders, so nothing should ever arrive here.)"""
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {"raw": (await request.body())[:2000].decode("utf-8", "replace")}
        engine.repo.event(dt.datetime.now(dt.timezone.utc), "INFO", "dhan_postback",
                          "postback received (recorded, not acted upon)", payload)
        return {"received": True}

    # ── UI ────────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        return html.replace("__API_TOKEN__", api_token)

    @app.get("/static/{name}")
    def static(name: str):
        p = (UI_DIR / name).resolve()
        if not p.is_file() or UI_DIR.resolve() not in p.parents:
            raise HTTPException(404, "not found")
        media = {"js": "application/javascript", "css": "text/css", "svg": "image/svg+xml",
                 "png": "image/png"}.get(p.suffix.lstrip("."), "application/octet-stream")
        return FileResponse(str(p), media_type=media, headers={"Cache-Control": "no-cache"})

    return app
