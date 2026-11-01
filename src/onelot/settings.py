"""Process-wide settings accessors shared by the Backtest Lab, the data layer
and the research gate.

The live engine passes its `AppConfig` around explicitly. The Lab code (ported
from the Fyers project) was written against module-level accessors, and that
style is kept here because the Lab runs in worker threads and subprocesses
where threading a config object through every call adds nothing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import PROJECT_ROOT, AppConfig

CONFIG_DIR = PROJECT_ROOT / "config"
_override: dict[str, Any] = {"config_path": None, "project_root": None}


@lru_cache(maxsize=1)
def current_settings() -> AppConfig:
    return AppConfig.load(_override["config_path"], project_root=_override["project_root"])


def reload_settings(
    config_path: str | Path | None = None, project_root: Path | None = None
) -> AppConfig:
    _override["config_path"] = config_path
    _override["project_root"] = project_root
    current_settings.cache_clear()
    return current_settings()


def load_yaml(name: str) -> Any:
    root = _override["project_root"] or PROJECT_ROOT
    path = Path(root) / "config" / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_universe() -> dict:
    return load_yaml("universe.yaml")


def load_costs() -> dict:
    return load_yaml("costs.yaml")
