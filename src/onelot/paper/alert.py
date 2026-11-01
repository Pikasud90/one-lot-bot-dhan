"""Desk alert -> the PENDING_FILL row of a paper trade (US-001 AC1, AC5, AC6). Pure: no I/O.

The alert is the dict `Desk._raise_signal` builds (and stores as the desk_signals payload).
It is validated here and snapshotted deep and JSON-safe, so later desk mutations can never
leak into the book. Naive times are rejected, never guessed; levels become integer paise
once (ADR-001 amendment 5); the unit is one share or one lot of the ALERTED option, never
`risk.lots_per_trade` (the book never sees risk config at all).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from collections.abc import Mapping
from decimal import Decimal
from numbers import Integral, Real
from typing import Any, Literal

import numpy as np

from ..clock import IST
from ..config import PRODUCT_FOR_CATEGORY
from ..data.universe import InstrumentSpec
from .money import to_paise

log = logging.getLogger(__name__)

Grade = Literal["live", "delayed"]
# Fallback cost table per alert kind when the instrument spec is unknown (Lab naming).
PRODUCT_FOR_KIND = {
    "STOCK": PRODUCT_FOR_CATEGORY["STOCK"],
    "INDEX": PRODUCT_FOR_CATEGORY["OPTIONS"],
}
DIRECTIONS = ("long", "short")


class PaperAlertError(ValueError):
    """An alert the paper book cannot record: a missing field, a naive time, a bad level."""


def grade_for(source: str | None) -> Grade:
    """`live` iff the bars came from Dhan (`dhan`, `dhan-ws`, ...); anything else is delayed."""
    return "live" if (source or "").strip().lower().startswith("dhan") else "delayed"


def alert_id_of(alert: Mapping[str, Any]) -> str:
    """The alert's id, stripped; PaperAlertError when there is none."""
    raw = alert.get("id") if isinstance(alert, Mapping) else None
    if raw is None or not str(raw).strip():
        raise PaperAlertError("alert has no id")
    return str(raw).strip()


def alert_from_desk_signal(signal_id: str, raw: object) -> dict[str, Any]:
    """The alert stored in a desk_signals payload, keyed by the ROW's id.

    `raw` is the payload's JSON text, parsed here so that one corrupt row fails alone.
    desk_signals.id is the key the reconciliation joins on, so it wins over the payload's.
    """
    payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(payload, dict):
        raise PaperAlertError(f"desk signal {signal_id}: payload is {type(payload).__name__}")
    stored_id = payload.get("id")
    if stored_id is not None and str(stored_id).strip() != signal_id:
        log.warning(
            "paper: desk signal %s carries payload id %r; using the row id", signal_id, stored_id
        )
    return {**payload, "id": signal_id}


def trade_values(
    alert_id: str,
    alert: Mapping[str, Any],
    specs: Mapping[str, InstrumentSpec],
    now: dt.datetime,
) -> dict[str, Any]:
    """Validate `alert` and build its PENDING_FILL `paper_trades` row (without the id)."""
    snapshot = jsonable(alert)  # a deep, JSON-safe copy: later desk mutations can't leak
    kind = _req_str(snapshot, "kind", alert_id)
    if kind not in PRODUCT_FOR_KIND:
        raise PaperAlertError(f"alert {alert_id}: kind={kind!r} is not STOCK or INDEX")
    direction = _req_str(snapshot, "direction", alert_id)
    if direction not in DIRECTIONS:
        raise PaperAlertError(f"alert {alert_id}: direction={direction!r} is not long/short")
    created_at = _aware_ts(snapshot.get("created_at"), "created_at", alert_id)
    expires_at = _aware_ts(snapshot.get("expires_at"), "expires_at", alert_id)
    if expires_at < created_at:
        raise PaperAlertError(f"alert {alert_id}: expires_at is before created_at")
    instrument_key = _req_str(snapshot, "instrument_key", alert_id)
    eco = snapshot.get("economics")
    if not isinstance(eco, dict):
        raise PaperAlertError(f"alert {alert_id}: economics missing")
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
    source = str(data.get("source") or "")
    spec = specs.get(instrument_key)
    # The cost table cost_for(instrument_key) charges from; by kind if the spec is unknown.
    product = spec.product if spec is not None and spec.product else PRODUCT_FOR_KIND[kind]
    return {
        "alert_id": alert_id,
        "status": "PENDING_FILL",
        "close_reason": None,
        "grade": grade_for(source),
        "data_source": source,
        "instrument_key": instrument_key,
        "series_key": _req_str(snapshot, "series_key", alert_id),
        "kind": kind,
        "side": _req_str(snapshot, "side", alert_id),
        "direction": direction,
        "strategy": _req_str(snapshot, "strategy", alert_id),
        "timeframe": _req_str(snapshot, "timeframe", alert_id),
        "confidence": _confidence_score(snapshot.get("confidence")),
        "product": product,
        "qty": unit_qty(kind, eco, spec, alert_id),
        "resolution_min": resolution_min(data.get("resolution")),
        "created_at": created_at,
        "expires_at": expires_at,
        "plan": {"alert": snapshot, "levels": levels_paise(eco, alert_id)},
        "updated_at": now,
    }


def unit_qty(kind: str, eco: Mapping[str, Any], spec: InstrumentSpec | None, alert_id: str) -> int:
    """One share for a stock; one lot of the alerted option for an index. Never sized.

    The alerted option's lot is the unit (Manager decision 12). A lot that disagrees with
    spec.lot_size (an exchange lot revision the specs missed) is kept and logged.
    """
    if kind == "STOCK":
        return 1
    option = eco.get("option")
    lot = option.get("lot") if isinstance(option, Mapping) else None
    if lot is None and spec is not None:
        lot = spec.lot_size
    size = float(lot) if isinstance(lot, Real) and not isinstance(lot, bool) else math.nan
    if not (size.is_integer() and size > 0):
        raise PaperAlertError(f"alert {alert_id}: no usable option lot size ({lot!r})")
    if spec is not None and spec.lot_size and int(size) != int(spec.lot_size):
        log.warning(
            "paper: alert %s: option lot %d differs from %s lot_size %d; keeping the alert's lot",
            alert_id,
            int(size),
            spec.key,
            int(spec.lot_size),
        )
    return int(size)


def levels_paise(eco: Mapping[str, Any], alert_id: str) -> dict[str, int | None]:
    """The alert's underlying levels converted to paise once (ADR-001 amendment 5)."""
    out: dict[str, int | None] = {}
    for name, required in (
        ("underlying_entry", True),
        ("underlying_stop", True),
        ("underlying_target", False),
    ):
        value = eco.get(name)
        if value is None and not required:
            out[f"{name}_paise"] = None
            continue
        try:
            out[f"{name}_paise"] = to_paise(value)
        except ValueError as exc:
            raise PaperAlertError(f"alert {alert_id}: economics.{name}: {exc}") from exc
    return out


def resolution_min(value: object) -> int | None:
    """'1m' -> 1, '2m' -> 2; anything unreadable -> None (step infers it from the bars)."""
    text = str(value or "").strip().lower()
    if text.endswith("m") and text[:-1].isdigit() and int(text[:-1]) > 0:
        return int(text[:-1])
    return None


def jsonable(value: object) -> Any:
    """A deep, JSON-safe copy: numpy scalars -> Python, NaN/inf -> None, times -> ISO."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Real):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    return str(value)


def _req_str(alert: Mapping[str, Any], key: str, alert_id: str) -> str:
    value = alert.get(key)
    if value is None or not str(value).strip():
        raise PaperAlertError(f"alert {alert_id}: {key} missing")
    return str(value)


def _aware_ts(value: object, name: str, alert_id: str) -> dt.datetime:
    """An ISO-8601 string (or datetime) with an offset -> IST. Naive input is rejected."""
    if isinstance(value, dt.datetime):
        ts = value
    elif isinstance(value, str):
        try:
            ts = dt.datetime.fromisoformat(value)
        except ValueError as exc:
            raise PaperAlertError(f"alert {alert_id}: {name}={value!r} is not ISO-8601") from exc
    else:
        raise PaperAlertError(f"alert {alert_id}: {name} missing")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise PaperAlertError(
            f"alert {alert_id}: {name}={value!r} has no timezone; refusing to guess"
        )
    return ts.astimezone(IST)


def _confidence_score(conf: object) -> float | None:
    score = conf.get("score") if isinstance(conf, Mapping) else conf
    if isinstance(score, Real) and not isinstance(score, bool) and math.isfinite(float(score)):
        return float(score)
    return None
