"""Entry point: `python -m onelot <command>`.

    run        start the engine + web app (default when no command is given)
    backfill   download / top up the historical corpus (Yahoo now, Dhan later)
    backtest   run one backtest from the shell
    research   run the walk-forward research gate across strategies/instruments
    quality    data coverage and quality report
    preflight  check which Dhan endpoints this token can reach
    cost       one-lot cost preview

The engine is ONE process that owns all state and serves the API and the
built-in web app. It never places an order.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .config import AppConfig, Secrets
from .logging_setup import setup_logging

log = logging.getLogger("onelot")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m onelot",
        description="One Lot Bot — signal-only, human-in-the-loop trade desk on Dhan. "
        "This program never places an order.",
    )
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="start the engine and the web app (default)")
    r.add_argument("--mock", action="store_true", help="synthetic market (no credentials needed)")
    r.add_argument("--port", type=int, default=None, help="override the API port")
    r.add_argument("--host", default=None, help="override the bind host")
    r.add_argument(
        "--lan",
        action="store_true",
        help="bind on 0.0.0.0 so other devices on your network can open the app",
    )
    r.add_argument("--no-api", action="store_true", help="run headless")

    b = sub.add_parser("backfill", help="download historical bars into the corpus")
    b.add_argument("--provider", choices=["auto", "dhan", "yahoo"], default=None)
    b.add_argument("--series", nargs="*", help="series keys (BANKNIFTY RELIANCE USDINR ...)")
    b.add_argument("--resolutions", nargs="*")
    b.add_argument("--start")
    b.add_argument("--full", action="store_true", help="ignore what is stored and re-download")

    bt = sub.add_parser("backtest", help="run one backtest")
    bt.add_argument("--strategy", required=True)
    bt.add_argument(
        "--instrument", required=True, help="e.g. BANKNIFTY_OPT, NIFTY_FUT, RELIANCE, USDINR"
    )
    bt.add_argument("--timeframe", default=None)
    bt.add_argument("--start")
    bt.add_argument("--end")
    bt.add_argument("--params", default="{}", help="JSON dict of strategy params")
    bt.add_argument("--export", help="folder to export CSV/JSON into")

    rs = sub.add_parser("research", help="walk-forward research gate")
    rs.add_argument("--strategies", nargs="*")
    rs.add_argument("--instruments", nargs="*", default=["BANKNIFTY_OPT"])
    rs.add_argument("--timeframes", default="5m,15m")
    rs.add_argument("--trials", type=int, default=30)
    rs.add_argument("--workers", type=int, default=None)

    sw = sub.add_parser(
        "sweep", help="brute-force every strategy x instrument x timeframe; saves every run"
    )
    sw.add_argument("--strategies", default="all", help="comma list or 'all'")
    sw.add_argument(
        "--instruments", default="focus", help="comma list or 'focus' (the desk watchlist)"
    )
    sw.add_argument("--timeframes", default="5m,15m,30m,60m")
    sw.add_argument("--cost-x", type=float, default=1.0, help="cost stress multiplier")
    sw.add_argument("--top", type=int, default=25, help="rows of the leaderboard to print")

    lb = sub.add_parser("leaderboard", help="rank every saved backtest run")
    lb.add_argument("--strategy")
    lb.add_argument("--instrument")
    lb.add_argument("--timeframe")
    lb.add_argument("--kind", choices=["single", "walkforward"])
    lb.add_argument("--min-trades", type=int, default=10)
    lb.add_argument("--top", type=int, default=40)

    q = sub.add_parser("quality", help="corpus coverage + quality report")
    q.add_argument("--resolution", default="5m")

    sub.add_parser("preflight", help="which Dhan endpoints can this token reach?")
    sub.add_parser("cost", help="one-lot cost preview (python -m onelot.cost_preview)")
    return p


# ── commands ──────────────────────────────────────────────────────────────


def cmd_run(args, cfg: AppConfig, secrets: Secrets) -> int:
    if args.lan:
        cfg = cfg.model_copy(
            update={
                "api": cfg.api.model_copy(update={"host": "0.0.0.0", "allow_non_loopback": True})
            }
        )
    host = args.host or cfg.api.host
    port = args.port or cfg.api.port

    print("=" * 72)
    print("  ONE LOT BOT — Dhan trade desk")
    print("  SIGNAL-ONLY. This program places no orders. You execute manually.")
    print("=" * 72)
    print(f"  mode      : {cfg.mode}")
    print(
        f"  broker    : {'MOCK (synthetic)' if args.mock or not secrets.has_broker_creds else 'Dhan'}"
        + ("  + Dhan account reads" if secrets.has_broker_creds else "")
    )
    print(f"  database  : {cfg.db_url_resolved.split('///')[-1]}")
    if not args.no_api:
        print(
            f"  web app   : http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/"
            + ("   (also reachable on your LAN IP)" if host == "0.0.0.0" else "")
        )
    print("=" * 72)

    if cfg.mode == "LIVE":
        log.warning("Running in LIVE mode — the journal will record real money.")

    from .engine import Engine
    from .lock import AlreadyRunningError

    engine = Engine(cfg, secrets, use_mock=args.mock)
    try:
        engine.start()
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return 2
    except Exception:
        log.exception("engine failed to start")
        engine.stop()
        return 1

    for w in engine.status.warnings:
        log.warning("STARTUP WARNING: %s", w)

    stop_event = threading.Event()

    def handle_signal(_sig, _frm):
        log.info("shutdown requested")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.no_api:
        try:
            stop_event.wait()
        finally:
            engine.stop()
        return 0

    import uvicorn

    from .api.server import create_app

    app = create_app(engine, secrets.api_token)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "API is bound to %s — anyone who can reach this host can see your "
            "positions; mutations still need the API token.",
            host,
        )

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    def serve():
        try:
            server.run()
        except Exception:
            log.exception("API server crashed")
        finally:
            stop_event.set()

    t = threading.Thread(target=serve, name="api", daemon=True)
    t.start()
    log.info("Web app ready at http://%s:%d/", host, port)

    try:
        stop_event.wait()
    finally:
        server.should_exit = True
        engine.stop()
    return 0


def cmd_backfill(args, cfg: AppConfig, secrets: Secrets) -> int:
    import datetime as dt

    from .data.backfill import backfill, choose_provider
    from .data.db import get_engine
    from .data.universe import sync_universe_table
    from .ratelimit import RateLimiter

    eng = get_engine(cfg.db_url_resolved)
    sync_universe_table(eng)
    adapter = None
    if (args.provider or cfg.data.provider) in ("dhan", "auto") and secrets.has_broker_creds:
        try:
            from .broker.dhan import DhanAdapter

            adapter = DhanAdapter(secrets.client_id, secrets.data_token, RateLimiter())
            if not adapter.data_api_status().get("active"):
                print("Dhan Data API plan is not active on this account; using Yahoo.")
                adapter = None
        except Exception as exc:  # noqa: BLE001
            print(f"Dhan unavailable ({exc}); using Yahoo.")
            adapter = None
    provider = choose_provider(args.provider, adapter)
    res = backfill(
        eng,
        provider,
        args.series,
        args.resolutions,
        start=dt.date.fromisoformat(args.start) if args.start else None,
        full=args.full,
        progress=lambda f, m: print(f"  {f * 100:5.1f}%  {m}"),
    )
    print(f"provider={res.provider} rows={res.rows} errors={len(res.errors)}")
    for e in res.errors:
        print("  ERROR", e)
    return 0 if not res.errors else 1


def cmd_backtest(args, cfg: AppConfig, secrets: Secrets) -> int:
    from .lab.report import export_result
    from .lab.runner import BacktestConfig, run_backtest, summarize

    params = json.loads(args.params or "{}")
    c = BacktestConfig.from_settings(
        args.strategy,
        args.instrument.upper(),
        params=params,
        timeframe=args.timeframe or cfg.backtest.default_timeframe,
        start=args.start,
        end=args.end,
    )
    res = run_backtest(c)
    print(summarize(res))
    if args.export:
        for p in export_result(res, args.export):
            print("exported:", p)
    return 0


def cmd_research(args, cfg: AppConfig, secrets: Secrets) -> int:
    from .lab.research import run_research

    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    results, path = run_research(
        args.strategies,
        tfs,
        [i.upper() for i in args.instruments],
        trials=args.trials,
        workers=args.workers,
        progress=lambda f, m: print(f"  {f * 100:5.1f}%  {m}"),
    )
    print(f"report: {path}")
    for r in results:
        o = r["oos"]
        print(
            f"  {r['strategy']:<18} {r['instrument']:<14} {r['timeframe']:<4} {r['verdict']:<5} "
            f"OOS trades={o.get('trades')} PF={o.get('profit_factor')} expR={o.get('expectancy_r')}"
        )
    return 0


def _print_board(rows: list[dict], top: int) -> None:
    print(
        f"{'#':>4} {'strategy':<18} {'instrument':<15} {'tf':<4} {'kind':<5} {'trades':>6} {'win%':>6} "
        f"{'PF':>6} {'expR':>7} {'net ₹':>10} {'maxDD ₹':>9} {'score':>7}"
    )
    for r in rows[:top]:
        print(
            f"{r['run_id']:>4} {r['strategy']:<18} {r['instrument']:<15} {r['timeframe']:<4} "
            f"{'OOS' if r['oos'] else 'IS':<5} {r['trades'] or 0:>6} {(r['win_rate'] or 0):>6.1f} "
            f"{(r['profit_factor'] or 0):>6.2f} {(r['expectancy_r'] or 0):>7.3f} {(r['net_pnl'] or 0):>10,.0f} "
            f"{(r['max_drawdown_inr'] or 0):>9,.0f} {(r['score'] if r['score'] is not None else float('nan')):>7.2f}"
        )


def cmd_sweep(args, cfg: AppConfig, secrets: Secrets) -> int:
    import time

    from .data.db import get_engine
    from .lab.jobs import JobRunner
    from .lab.sweep import leaderboard, plan, summarise

    strategies = (
        None
        if args.strategies == "all"
        else [s.strip() for s in args.strategies.split(",") if s.strip()]
    )
    instruments = (
        None
        if args.instruments == "focus"
        else [i.strip().upper() for i in args.instruments.split(",") if i.strip()]
    )
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    combos = plan(strategies, instruments, tfs)
    print(
        f"sweep: {len(combos)} runs ({len({c['strategy'] for c in combos})} strategies x "
        f"{len({c['instrument_key'] for c in combos})} instruments x {len(tfs)} timeframes)"
    )
    eng = get_engine(cfg.db_url_resolved)
    jobs = JobRunner(eng, workers=1)
    rid = jobs.submit_sweep(combos, label="cli sweep", base={"cost_multiplier": args.cost_x})
    last = ""
    while True:
        run = jobs.get_run(rid, with_trades=False)
        if run["message"] != last:
            last = run["message"] or ""
            print(f"  {run['progress'] * 100:5.1f}%  {last}")
        if run["status"] in ("DONE", "FAILED"):
            break
        time.sleep(2)
    jobs.pool.shutdown(wait=True)
    print(f"sweep #{rid}: {run['status']} — {run['metrics']}")
    rows = leaderboard(eng, sweep_id=rid)
    s = summarise(rows)
    print(f"ranked {s['ranked']} / {s['runs']}; {s['positive']} with PF >= 1.2 and positive score")
    _print_board(rows, args.top)
    return 0 if run["status"] == "DONE" else 1


def cmd_leaderboard(args, cfg: AppConfig, secrets: Secrets) -> int:
    from .data.db import get_engine
    from .lab.sweep import leaderboard, summarise

    rows = leaderboard(
        get_engine(cfg.db_url_resolved),
        strategy=args.strategy,
        instrument=args.instrument,
        timeframe=args.timeframe,
        kind=args.kind,
        min_trades=args.min_trades,
    )
    s = summarise(rows)
    print(
        f"{s['runs']} saved runs, {s['ranked']} ranked (>= {args.min_trades} trades), {s['positive']} positive"
    )
    _print_board(rows, args.top)
    return 0


def cmd_quality(args, cfg: AppConfig, secrets: Secrets) -> int:
    import pandas as pd

    from .data.backfill import quality_report
    from .data.db import get_engine
    from .data.store import coverage_report

    eng = get_engine(cfg.db_url_resolved)
    with pd.option_context(
        "display.width", 200, "display.max_columns", 20, "display.max_rows", 500
    ):
        print(coverage_report(eng))
        print()
        print(quality_report(eng, args.resolution))
    return 0


def cmd_preflight(args, cfg: AppConfig, secrets: Secrets) -> int:
    from .preflight import main as preflight_main

    return preflight_main()


def cmd_cost(args, cfg: AppConfig, secrets: Secrets) -> int:
    from .cost_preview import main as cost_main

    return cost_main([])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `python -m onelot --mock` keeps working: treat bare flags as `run`.
    known = {
        "run",
        "backfill",
        "backtest",
        "research",
        "quality",
        "preflight",
        "cost",
        "-h",
        "--help",
    }
    if not argv or all(a.startswith("-") for a in argv) and not (set(argv) & known):
        argv = ["run", *argv]
    elif argv[0] not in known and argv[0].startswith("-") and len(argv) > 1 and argv[1] in known:
        pass
    args = build_parser().parse_args(argv)
    cfg = AppConfig.load(args.config)
    secrets = Secrets.load(cfg.project_root)
    component = args.cmd or "run"
    setup_logging(
        level=args.log_level or cfg.logging.level,
        log_dir=cfg.log_dir,
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
        secrets=secrets.secret_values(),
        component="engine" if component == "run" else component,
    )
    fn = {
        "run": cmd_run,
        "backfill": cmd_backfill,
        "backtest": cmd_backtest,
        "research": cmd_research,
        "sweep": cmd_sweep,
        "leaderboard": cmd_leaderboard,
        "quality": cmd_quality,
        "preflight": cmd_preflight,
        "cost": cmd_cost,
    }[component]
    return int(fn(args, cfg, secrets) or 0)


if __name__ == "__main__":
    sys.exit(main())
