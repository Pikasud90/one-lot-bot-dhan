"""The Simulator's routes (`.agile/stories/SIM-API.md`). Reads are open; every POST needs the
token. Errors: 400 with a plain sentence, 404 for an unknown session, 409 when the action
does not fit the state. Answers are strict JSON (no NaN)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response

from .engine import SimError
from .manager import SimManager

#: (the running desk or None) -> a manager; tests pass their own
ManagerFactory = Callable[[Any], SimManager]
#: an optional JSON body (FastAPI reads it; a module-level default, as ruff B008 asks)
_BODY = Body(None)


def _json(obj: Any) -> Response:
    return Response(json.dumps(obj, allow_nan=False), media_type="application/json")


def _body(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise HTTPException(400, "The request body must be a JSON object.")
    return payload


def register(
    app: FastAPI,
    engine: Any,
    require_token: Callable[..., None],
    factory: ManagerFactory | None = None,
) -> None:
    """Add `/api/sim/*` to `app`. One manager per running desk (`engine.desk`), built on first
    use; a session of an earlier manager is rebuilt from the tables when asked for."""
    holder: dict[str, Any] = {}
    lock = threading.Lock()

    def make(desk: Any) -> SimManager:
        if factory is not None:
            return factory(desk)
        return SimManager(engine.db.engine, desk=desk)

    def manager() -> SimManager:
        desk = getattr(engine, "desk", None)
        with lock:
            if "m" not in holder or holder.get("desk") is not desk:
                holder["m"] = make(desk)
                holder["desk"] = desk
            return holder["m"]

    def run(fn: Callable[[], Any]) -> Response:
        try:
            return _json(fn())
        except SimError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @app.get("/api/sim/options")
    def sim_options() -> Response:
        return run(lambda: manager().options())

    @app.get("/api/sim/days")
    def sim_days(instrument: str = Query("")) -> Response:
        return run(lambda: manager().days(instrument))

    @app.get("/api/sim/candles")
    def sim_candles(
        instrument: str = Query(""),
        timeframe: str = Query("3m"),
        until: str | None = Query(None),
        count: str | None = Query(None),
    ) -> Response:
        return run(lambda: manager().candles(instrument, timeframe, until, count))

    @app.get("/api/sim/prepare")
    def sim_prepare(
        instrument: str = Query(""),
        strategy: str = Query(""),
        timeframe: str | None = Query(None),
        start: str | None = Query(None),
    ) -> Response:
        query = {"instrument": instrument, "strategy": strategy, "timeframe": timeframe,
                 "start": start}  # fmt: skip
        return run(lambda: manager().prepare(query))

    @app.post("/api/sim/sessions", dependencies=[Depends(require_token)])
    def sim_create(payload: Any = _BODY) -> Response:
        body = _body(payload)
        m = manager()
        return run(lambda: m.create(body))

    @app.get("/api/sim/sessions/{sid}")
    def sim_get(sid: str) -> Response:
        m = manager()
        return run(lambda: m.read(sid))

    def action(sid: str, fn: Callable[[Any], Any]) -> Response:
        m = manager()
        return run(lambda: m.act(sid, fn))

    @app.post("/api/sim/sessions/{sid}/advance", dependencies=[Depends(require_token)])
    def sim_advance(sid: str, payload: Any = _BODY) -> Response:
        body = _body(payload)
        raw = body.get("minutes", 1)
        whole = isinstance(raw, int) and not isinstance(raw, bool)
        if isinstance(raw, float) and raw.is_integer():
            raw, whole = int(raw), True
        if not whole or not 1 <= raw <= 390:
            raise HTTPException(400, "minutes must be a whole number from 1 to 390.")
        n = int(raw)
        until = body.get("until")
        if until not in (None, "event", "alert"):
            raise HTTPException(400, 'until must be null, "event" or "alert".')
        return action(sid, lambda s: s.advance(n, until))

    @app.post("/api/sim/sessions/{sid}/take", dependencies=[Depends(require_token)])
    def sim_take(sid: str, payload: Any = _BODY) -> Response:
        alert_id = str(_body(payload).get("alert_id") or "")
        return action(sid, lambda s: s.take(alert_id))

    @app.post("/api/sim/sessions/{sid}/skip", dependencies=[Depends(require_token)])
    def sim_skip(sid: str, payload: Any = _BODY) -> Response:
        alert_id = str(_body(payload).get("alert_id") or "")
        return action(sid, lambda s: s.skip(alert_id))

    @app.post("/api/sim/sessions/{sid}/close", dependencies=[Depends(require_token)])
    def sim_close(sid: str, payload: Any = _BODY) -> Response:
        return action(sid, lambda s: s.close())

    @app.post("/api/sim/sessions/{sid}/end", dependencies=[Depends(require_token)])
    def sim_end(sid: str, payload: Any = _BODY) -> Response:
        return action(sid, lambda s: s.end())

    @app.get("/api/sim/trades")
    def sim_trades(
        session_id: str | None = Query(None), limit: int = Query(200, ge=1, le=5000)
    ) -> Response:
        return run(lambda: {"trades": manager().trades(session_id or None, limit)})

    @app.get("/api/sim/trades.csv")
    def sim_trades_csv(session_id: str | None = Query(None)) -> Response:
        text = manager().trades_csv(session_id or None)
        return Response(
            text,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="sim_trades.csv"'},
        )
