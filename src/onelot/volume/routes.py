"""The Volume Watch's routes (`.agile/stories/VOL-API.md`): four reads, open like the others.
Errors: 400 with a plain sentence. Answers are strict JSON (no NaN)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from .service import VolumeError, VolumeWatch

#: (the running desk or None) -> a watch; tests pass their own
WatchFactory = Callable[[Any], VolumeWatch]


def _json(obj: Any) -> Response:
    return Response(json.dumps(obj, allow_nan=False), media_type="application/json")


def register(app: FastAPI, engine: Any, factory: WatchFactory | None = None) -> None:
    """Add `/api/volume*` to `app`. The running desk's own watch when it has one (the one its
    poll steps), else one watch over the store, built on first use."""
    holder: dict[str, Any] = {}
    lock = threading.Lock()

    def watch() -> VolumeWatch:
        desk = getattr(engine, "desk", None)
        own = getattr(desk, "volume", None) if desk is not None else None
        if isinstance(own, VolumeWatch):
            return own
        with lock:
            if "w" not in holder or holder.get("desk") is not desk:
                holder["w"] = (
                    factory(desk) if factory is not None else VolumeWatch(engine.db.engine, desk)
                )
                holder["desk"] = desk
            return holder["w"]

    def run(fn: Callable[[], Any]) -> Response:
        try:
            return _json(fn())
        except VolumeError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @app.get("/api/volume")
    def volume(
        date: str | None = Query(None), index: str = Query("NIFTY", max_length=32)
    ) -> Response:
        return run(lambda: watch().body(date or None, index))

    @app.get("/api/volume/profile")
    def volume_profile() -> Response:
        return run(lambda: watch().profile())

    @app.get("/api/volume/alerts")
    def volume_alerts(limit: int = Query(100, ge=1, le=1000)) -> Response:
        return run(lambda: watch().alerts(limit))

    @app.get("/api/volume/study")
    def volume_study() -> Response:
        return run(lambda: watch().study())
