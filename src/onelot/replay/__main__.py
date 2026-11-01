"""`python -m onelot.replay` — replay the strategy over a synthetic tape.

Needs no credentials and touches no network. Use it to calibrate parameters
BEFORE running live, and to check that a config change has not silently turned
the signal rate to zero.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..config import AppConfig
from ..logging_setup import setup_logging
from .replay import ReplayConfig, ReplayRunner


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m onelot.replay")
    p.add_argument("--sessions", type=int, default=40)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument(
        "--drift", type=float, default=0.0, help="per-session log drift; negative for a bear tape"
    )
    p.add_argument("--trend", type=float, default=0.006)
    p.add_argument(
        "--capital",
        type=float,
        default=None,
        help="override capital; default is config account.capital_inr",
    )
    p.add_argument("--ivp-mode", default="degraded", choices=["strict", "degraded", "off"])
    p.add_argument("--step", type=int, default=60, help="simulation step, seconds")
    p.add_argument("--config", default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    setup_logging(level="ERROR" if args.quiet else "WARNING")
    logging.getLogger("onelot").setLevel(logging.ERROR if args.quiet else logging.WARNING)

    cfg = AppConfig.load(args.config)
    rc = ReplayConfig(
        sessions=args.sessions,
        seed=args.seed,
        drift_per_session=args.drift,
        trend_strength=args.trend,
        capital=args.capital,
        ivp_mode=args.ivp_mode,
        step_seconds=args.step,
    )

    cap = args.capital if args.capital is not None else cfg.account.capital_inr
    lots = cfg.risk.lots_per_trade
    mode = f"{lots} lot"
    print(
        f"Replaying {args.sessions} sessions (seed={args.seed}, drift={args.drift}, "
        f"capital=Rs {cap:,.0f}, {mode}, ivp={args.ivp_mode})..."
    )
    print("NOTE: this is a SYNTHETIC tape. It validates the machinery and the")
    print("      cost model end to end; it says nothing about real-market edge.\n")

    result = ReplayRunner(cfg, rc).run()
    print(result.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
