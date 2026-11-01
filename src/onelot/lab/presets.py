"""Three parameter settings per strategy for the brute-force matrix.

The research search spaces (lab/research.py::SPACES) already declare, per strategy,
the handful of indicator knobs that matter and the honest choices for each. The
matrix uses them to build three fixed presets - chosen BEFORE looking at any
result, so a preset can never be tuned on the data it is scored on:

    low      the first (tightest / fastest / most conservative) choice of every knob
    default  the strategy's shipped defaults
    high     the last (loosest / slowest / most aggressive) choice of every knob

Exit knobs (target_r, trail_atr_mult) stay at the defaults for all three, so the
presets vary the ENTRY logic only; the exit geometry is studied separately.
"""

from __future__ import annotations

from .research import EXITS, SPACES

PRESET_NAMES = ("low", "default", "high")
_EXIT_KEYS = set(EXITS)


def presets_for(strategy: str) -> dict[str, dict]:
    space = SPACES.get(strategy) or {}
    knobs = {
        k: v
        for k, v in space.items()
        if k not in _EXIT_KEYS and isinstance(v, dict) and v.get("choices")
    }
    low = {k: v["choices"][0] for k, v in knobs.items()}
    high = {k: v["choices"][-1] for k, v in knobs.items()}
    return {"low": low, "default": {}, "high": high}


def all_presets() -> dict[str, dict[str, dict]]:
    return {s: presets_for(s) for s in SPACES if s != "benchmark_random"}
