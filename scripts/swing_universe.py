"""US-093: delivery setups on a wide stock universe (study, 2026-11-24). Research only.

Stages (run from the MAIN checkout, see .agile/stories/RESEARCH-RULES.md section 1):

    universe  Dhan's instrument master through the app's adapter, symbols to security ids
    fetch     daily bars of every stock through the app's adapter, one request a second, cached
    check     first bars, one-day moves over 35%, Dhan against the alpha study's Yahoo bars
    p1 m1 m2 m3 r1   one stage per setup: its trades (whole shares near Rs 10,000, all costs)
    select    the selection rule of RESEARCH-RULES section 5, cells counted, placebo of labels
    placebo   random entry dates in the same stocks and years, same holding days, 200 runs
    report    every table of the note
    lookahead truncation test of every signal function on random signals

Outputs: data/research/swing_2026-11-24/ (git-ignored). The live store is read only (NIFTY).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# the harness of the MAIN checkout (cwd), never the worktree's copy beside this file
sys.path.insert(0, str(Path.cwd() / "scripts"))
import mtf_bruteforce as h  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

ROOT = Path.cwd().resolve()
OUT = ROOT / "data" / "research" / "swing_2026-11-24"
DB = OUT / "daily.sqlite"
YAHOO = ROOT / "data" / "research" / "alpha_2026-11-12" / "daily_universe"
WT = Path(__file__).resolve().parents[1]

FETCH_FROM, FETCH_TO = dt.date(2010, 1, 1), dt.date(2026, 11, 23)
LAST = pd.Timestamp("2026-11-07")  # bars after this are not used
PERIODS = (("TUNE", None, "2024-11-07"), ("CHOOSE", "2024-11-08", "2025-11-07"),
           ("TEST", "2025-11-08", "2026-11-07"))  # fmt: skip
MIN_SPAN_START = pd.Timestamp("2023-11-08")  # a stock needs bars from before this date
UNIT = 10000.0
SLIP = h.STOCK_SLIPPAGE
DP = h.DP_CHARGE_INR

# ───────────────────────────── the universe, written before any data ─────────────────────────────

DESK18 = list(h.STOCKS)
ALPHA_B = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "BAJAJ-AUTO", "BAJAJFINSV", "BEL",
           "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM",
           "HCLTECH", "HINDALCO", "HINDUNILVR", "INDUSINDBK", "JSWSTEEL", "NESTLEIND", "NTPC",
           "ONGC", "POWERGRID", "SHRIRAMFIN", "TATACONSUM", "TECHM", "TITAN", "ULTRACEMCO",
           "WIPRO"]  # fmt: skip
ALPHA_C = ["ACC", "AMBUJACEM", "BHEL", "DLF", "GAIL", "JINDALSTEL", "PNB", "RELINFRA", "RPOWER",
           "SAIL", "SIEMENS", "TATAPOWER", "VEDL"]  # fmt: skip
# Large and mid-size liquid NSE names (NIFTY 50, Next 50, Midcap 100 and F&O lists as known on
# 2026-11-24), by sector. Written before any bar was fetched; ADANIGREEN excluded.
NEW = [
    # banks and finance
    "HDFCLIFE", "SBILIFE", "JIOFIN", "BANKBARODA", "CANBK", "UNIONBANK", "INDIANB", "BANKINDIA",
    "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "BANDHANBNK", "YESBANK", "RBLBANK", "CHOLAFIN",
    "BAJAJHLDNG", "MUTHOOTFIN", "MANAPPURAM", "LICHSGFIN", "M&MFIN", "SBICARD", "HDFCAMC",
    "ABCAPITAL", "PFC", "RECLTD", "ICICIGI", "ICICIPRULI", "MFSL", "LTF", "PEL", "IIFL",
    "ANGELONE", "BSE", "MCX", "CDSL", "IRFC", "LICI", "POONAWALLA",
    # autos
    "TVSMOTOR", "ASHOKLEY", "BALKRISIND", "BHARATFORG", "ESCORTS", "EXIDEIND", "MOTHERSON",
    "MRF", "APOLLOTYRE", "TIINDIA", "BOSCHLTD", "UNOMINDA", "SONACOMS",
    # capital goods and infrastructure
    "ABB", "HAL", "BDL", "MAZDOCK", "COCHINSHIP", "CGPOWER", "CUMMINSIND", "POLYCAB", "KEI",
    "SUZLON", "THERMAX", "GMRAIRPORT", "IRB", "NBCC", "NCC", "RVNL", "CONCOR", "INDIGO",
    # energy and utilities
    "HINDPETRO", "IOC", "PETRONET", "IGL", "MGL", "GUJGASLTD", "OIL", "NHPC", "SJVN",
    "TORNTPOWER", "JSWENERGY", "ADANIPOWER", "NLCINDIA", "CESC", "ADANIENSOL",
    # metals and mining
    "NATIONALUM", "NMDC", "HINDZINC", "JSL", "APLAPOLLO", "HINDCOPPER",
    # cement and materials
    "SHREECEM", "DALBHARAT", "RAMCOCEM", "JKCEMENT", "PIDILITIND", "BERGEPAINT", "KANSAINER",
    "ASTRAL", "SUPREMEIND",
    # consumer
    "DABUR", "MARICO", "GODREJCP", "COLPAL", "UNITDSPR", "UBL", "VBL", "JUBLFOOD", "PAGEIND",
    "BATAINDIA", "VOLTAS", "HAVELLS", "CROMPTON", "WHIRLPOOL", "BLUESTARCO", "DIXON", "TRENT",
    "DMART", "INDHOTEL", "IRCTC", "ETERNAL", "KALYANKJIL", "NAUKRI",
    # IT and telecom
    "LTIM", "MPHASIS", "COFORGE", "PERSISTENT", "LTTS", "TATAELXSI", "OFSS", "KPITTECH",
    "TATACOMM", "IDEA", "INDUSTOWER", "SUNTV", "ZEEL",
    # pharma and health
    "TORNTPHARM", "ZYDUSLIFE", "ALKEM", "LUPIN", "AUROPHARMA", "BIOCON", "GLENMARK", "IPCALAB",
    "LAURUSLABS", "ABBOTINDIA", "FORTIS", "MAXHEALTH", "SYNGENE", "GRANULES",
    # realty
    "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD", "LODHA",
    # chemicals and agri
    "SRF", "PIIND", "UPL", "DEEPAKNTR", "AARTIIND", "TATACHEM", "NAVINFLUOR", "CHAMBLFERT",
    "COROMANDEL",
]  # fmt: skip
GROUPS = {"desk18": DESK18, "alpha43": ALPHA_B + ALPHA_C, "new": NEW}
EXCLUDED = {"ADANIGREEN"}
# symbols renamed since the lists were written (found by company name in the master)
ALIASES = {"MM": "M&M", "LTIM": "LTM", "ICICIPRULI": "ICICILIFE"}


def group_of() -> dict[str, str]:
    out = {}
    for g, keys in GROUPS.items():
        for k in keys:
            out.setdefault(k, g)
    return out


# ───────────────────────────── the daily store of this study ─────────────────────────────


def db() -> sqlite3.Connection:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily (symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, PRIMARY KEY (symbol, date));
        CREATE TABLE IF NOT EXISTS fetched (symbol TEXT PRIMARY KEY, security_id TEXT,
            status TEXT, rows INTEGER, fetched_at TEXT);
        """
    )
    return con


def research_adapter():
    """The app's own Dhan adapter, built as scripts/expired_options_fetch.py builds it
    (credentials loaded by the app, never read or printed here), at one request a second."""
    from onelot.broker.dhan import DhanAdapter
    from onelot.config import Secrets
    from onelot.ratelimit import EndpointSpec, RateLimiter

    s = Secrets.load(ROOT)
    limiter = RateLimiter(
        (
            EndpointSpec("historical", rate_per_sec=1.0, capacity=1.0),
            EndpointSpec("reference", rate_per_sec=0.2, capacity=1.0),
        ),
        global_rate=1.0,
        global_capacity=1.0,
    )
    return DhanAdapter(s.client_id, s.data_token, limiter)


def stage_universe() -> None:
    """Dhan's instrument master (one download through the adapter, cached), NSE cash rows."""
    OUT.mkdir(parents=True, exist_ok=True)
    master = OUT / "master_nse_eq.csv"
    if not master.exists():
        ad = research_adapter()
        rows = ad._download_scrip_master()
        keep = [r for r in rows if str(r.get("EXCH_ID", r.get("SEM_EXM_EXCH_ID", ""))) == "NSE"
                and str(r.get("SEGMENT", r.get("SEM_SEGMENT", ""))) == "E"]  # fmt: skip
        pd.DataFrame(keep).to_csv(master, index=False)
        print(f"master: {len(rows)} rows, {len(keep)} NSE cash rows (1 request)")
    m = pd.read_csv(master, dtype=str)
    sym_col = "UNDERLYING_SYMBOL" if "UNDERLYING_SYMBOL" in m.columns else "SEM_TRADING_SYMBOL"
    sid_col = "SECURITY_ID" if "SECURITY_ID" in m.columns else "SEM_SMST_SECURITY_ID"
    ser_col = "SERIES" if "SERIES" in m.columns else "SEM_SERIES"
    m = m[m[ser_col].isin(["EQ", "BE"])].copy()
    m["_eq"] = (m[ser_col] == "EQ").astype(int)
    m = m.sort_values("_eq", ascending=False).drop_duplicates(sym_col)
    by_sym = dict(zip(m[sym_col].str.upper(), m[sid_col], strict=True))
    from onelot.data.universe import load_specs

    series, _ = load_specs()
    gmap = group_of()
    rows, missing = [], []
    for sym, grp in gmap.items():
        if sym in EXCLUDED:
            continue
        look = ALIASES.get(sym, sym)
        sid = by_sym.get(look)
        cfg = series.get(sym)
        if cfg is not None and cfg.dhan_security_id and sid and str(cfg.dhan_security_id) != sid:
            print(f"  {sym}: master {sid}, universe.yaml {cfg.dhan_security_id}: yaml kept")
            sid = str(cfg.dhan_security_id)
        if sid is None and cfg is not None and cfg.dhan_security_id:
            sid = str(cfg.dhan_security_id)
        if sid is None:
            missing.append(sym)
            continue
        rows.append({"symbol": sym, "group": grp, "security_id": sid})
    u = pd.DataFrame(rows)
    u.to_csv(OUT / "universe.csv", index=False)
    print(f"universe: {len(u)} stocks mapped " + str(u.group.value_counts().to_dict()))
    print("not in the master: " + (", ".join(missing) or "none"))


def stage_fetch() -> None:
    """Daily bars, one request a stock, never twice; stop after three refusals."""
    from onelot.broker.base import Contract
    from onelot.data.providers.dhan import _frame

    u = pd.read_csv(OUT / "universe.csv", dtype=str)
    con = db()
    done = {r[0] for r in con.execute("SELECT symbol FROM fetched")}
    todo = [r for r in u.itertuples() if r.symbol not in done]
    print(f"to fetch: {len(todo)} of {len(u)}")
    if not todo:
        return
    ad = research_adapter()
    fails, n_req = 0, 0
    for r in todo:
        if fails >= 3:
            print("three failures: no more requests")
            break
        c = Contract(security_id=str(r.security_id), exchange_segment="NSE_EQ", symbol=r.symbol,
                     underlying=r.symbol, instrument_type="EQUITY")  # fmt: skip
        t0 = time.monotonic()
        n_req += 1
        try:
            bars = ad.historical_daily(c, FETCH_FROM, FETCH_TO + dt.timedelta(days=1))
            df = _frame(bars, daily=True)
            status = "ok" if len(df) else "empty"
        except Exception as exc:  # noqa: BLE001
            fails += 1
            msg = f"{type(exc).__name__}: {str(exc)[:80]}"
            print(f"  {r.symbol}: failed ({msg})")
            df, status = pd.DataFrame(), "failed"
            if fails < 3:
                time.sleep(2.0)
            continue
        if len(df):
            d = df.copy()
            d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
            con.executemany(
                "INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?)",
                [(r.symbol, i.date().isoformat(), *map(float, x)) for i, x in
                 zip(d.index, d[["open", "high", "low", "close", "volume"]].to_numpy(), strict=True)],
            )  # fmt: skip
        con.execute(
            "INSERT OR REPLACE INTO fetched VALUES (?,?,?,?,?)",
            (r.symbol, str(r.security_id), status, len(df), dt.datetime.now().isoformat()),
        )
        con.commit()
        first = df.index[0].date() if len(df) else None
        print(f"  {r.symbol:12s} {status} {len(df):5d} bars from {first}", flush=True)
        time.sleep(max(0.0, 1.05 - (time.monotonic() - t0)))
    print(f"requests this run: {n_req}, failures: {fails}")


# ───────────────────────────── loading, checks ─────────────────────────────


# One-day moves over 35% that are corporate actions Dhan's daily bars do not adjust (stage
# `check`: demergers of 2015 and 2020, and two moves in the first year). The prices before
# the day are put on the later scale by that day's close ratio. Every other such move (bank
# recapitalisation of 2017, March 2020, Yes Bank, Vodafone Idea, Zee) is a real price move.
CORP_ACTIONS = {"ADANIENT": ["2015-06-03"], "JSL": ["2015-11-19"], "TATACHEM": ["2020-03-04"],
                "BRITANNIA": ["2010-03-08"], "TMPV": ["2011-01-03"]}  # fmt: skip


def load_raw() -> dict[str, pd.DataFrame]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = pd.read_sql("SELECT * FROM daily ORDER BY symbol, date", con, parse_dates=["date"])
    out = {}
    for sym, g in d.groupby("symbol"):
        f = g.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        f = f[(f.open > 0) & (f.high > 0) & (f.low > 0) & (f.close > 0)]
        for day in CORP_ACTIONS.get(str(sym), ()):
            i = f.index.get_loc(pd.Timestamp(day))
            k = f.close.iloc[i] / f.close.iloc[i - 1]
            f.iloc[:i, :4] = f.iloc[:i, :4] * k
            f.iloc[:i, 4] = f.iloc[:i, 4] / k
        out[str(sym)] = f[f.index <= LAST]
    return out


def jumps(f: pd.DataFrame, lim: float = 0.35) -> list[tuple[pd.Timestamp, float, float]]:
    """One-day close moves beyond `lim`, with the move over the next 20 sessions (a split
    that is not adjusted shows a jump that never reverses; a real crash may)."""
    r = f.close / f.close.shift(1) - 1
    out = []
    for d in r.index[r.abs() > lim]:
        i = f.index.get_loc(d)
        after = f.close.iloc[min(len(f) - 1, i + 20)] / f.close.iloc[i] - 1
        out.append((d, float(r.loc[d]), float(after)))
    return out


def nifty_close() -> pd.Series:
    """NIFTY's daily close: the live store (read only, by the harness's reader) and, before its
    first bar (2017-01-02), the alpha study's cached Yahoo bars of NIFTY (no new download)."""
    h._init_worker()
    b = h._cache.bars(h._cache.spec("NIFTY_OPT").series_key, "1d")
    idx = pd.DatetimeIndex(b.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    st = pd.Series(b["close"].to_numpy(float), index=idx.normalize())
    st = st[~st.index.duplicated(keep="last")]
    y = pd.read_csv(YAHOO / "NIFTY.csv", index_col=0, parse_dates=True)["close"].dropna()
    y.index = pd.DatetimeIndex(y.index).normalize()
    both = pd.concat([y[y.index < st.index[0]], st]).sort_index()
    return both[both.index <= LAST]


def stage_check() -> None:
    raw = load_raw()
    u = pd.read_csv(OUT / "universe.csv", dtype=str)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    fet = pd.read_sql("SELECT * FROM fetched", con)
    L = [f"fetched: {len(fet)} of {len(u)}; " + str(fet.status.value_counts().to_dict())]
    first = {s: f.index[0] for s, f in raw.items()}
    short = sorted(s for s, d in first.items() if d >= MIN_SPAN_START)
    L.append(f"bars from before {MIN_SPAN_START.date()} (kept): {len(raw) - len(short)}; "
             f"too short (dropped): {', '.join(short) or 'none'}")  # fmt: skip
    fy = pd.Series({s: d.year for s, d in first.items()}).value_counts().sort_index()
    L.append("first bar by year: " + " ".join(f"{y}:{n}" for y, n in fy.items()))
    L.append("one-day close moves beyond 35% (date, move, move over the next 20 sessions):")
    for s, f in sorted(raw.items()):
        j = jumps(f)
        if j:
            L.append(
                f"  {s:11s} " + "; ".join(f"{d.date()} {r:+.0%} then {a:+.0%}" for d, r, a in j)
            )
    # Dhan against Yahoo (alpha study cache) on the shared days: the ratio of the closes
    L.append("Dhan close / Yahoo close on shared days (median, 1st and 99th percentile; a "
             "split one source adjusts and the other does not moves the ratio):")  # fmt: skip
    for s in DESK18 + ALPHA_B + ALPHA_C:
        fy_ = YAHOO / f"{s}.csv"
        if s not in raw or not fy_.exists():
            continue
        y = pd.read_csv(fy_, index_col=0, parse_dates=True)["close"].dropna()
        y.index = pd.DatetimeIndex(y.index).normalize()
        r = (raw[s].close / y).dropna()
        q = r.quantile([0.01, 0.5, 0.99])
        flag = " <-" if q.iloc[2] / q.iloc[0] > 1.02 else ""
        L.append(
            f"  {s:11s} n {len(r):4d} {q.iloc[1]:.3f} [{q.iloc[0]:.3f}, {q.iloc[2]:.3f}]{flag}"
        )
    nc = nifty_close()
    L.append(f"NIFTY closes {nc.index[0].date()} to {nc.index[-1].date()}, {len(nc)} days")
    # the cost function against the product's delivery money module
    from onelot.delivery import money

    model = money.delivery_model(h._cost_cfg)
    dpp = money.depository_paise(h._cost_cfg)
    rng = np.random.default_rng(1)
    worst = 0.0
    for _ in range(200):
        bo, xo = rng.uniform(20, 30000), rng.uniform(20, 30000)
        q = max(1, math.floor(UNIT / bo))
        ef, xf = bo * (1 + SLIP), xo * (1 - SLIP)
        mine = float(net_pct(np.array([bo]), np.array([xo]), np.array([q]))[0])
        r = money.trade_result(model, money.rupees_to_paise(ef), money.rupees_to_paise(xf), q, dpp)
        worst = max(worst, abs(mine - r.net_pct))
    L.append(f"cost function against onelot.delivery.money on 200 random trades: largest "
             f"difference {worst:.4f} points (paise rounding)")  # fmt: skip
    (OUT / "check.txt").write_text("\n".join(L) + "\n")
    print("\n".join(L))


# ───────────────────────────── costs and the frames of the study ─────────────────────────────

_LC = None


def legcost():
    global _LC
    if _LC is None:
        if h._cost_cfg is None:
            h._init_worker()
        _LC = h.LegCost.from_config(h._cost_cfg.product("equity_delivery"))
    return _LC


def qty_for(close: np.ndarray) -> np.ndarray:
    """Whole shares that keep the amount at or just under Rs 10,000, at least one."""
    return np.maximum(1, np.floor(UNIT / np.asarray(close, float))).astype(int)


def net_pct(buy_open: np.ndarray, sell_raw: np.ndarray, qty: np.ndarray) -> np.ndarray:
    """Net % of the entry value: slippage both sides, delivery charges on both legs and the
    depository charge of the sale."""
    lc = legcost()
    ef, xf = np.asarray(buy_open) * (1 + SLIP), np.asarray(sell_raw) * (1 - SLIP)
    q = np.asarray(qty, float)
    net = (xf - ef) * q - lc.leg(ef * q, sell=False) - lc.leg(xf * q, sell=True) - DP
    return net / (ef * q) * 100.0


class Frame:
    """One stock's arrays, the causal indicators every setup reads, and NIFTY's filter."""

    def __init__(self, sym: str, f: pd.DataFrame, nifty_ok: pd.Series) -> None:
        self.sym = sym
        self.dates = f.index
        self.o, self.hi, self.lo, self.c, self.v = (
            f[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")
        )
        self.n = len(f)
        self.mkt = nifty_ok.reindex(f.index).fillna(False).to_numpy(bool)


def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def prior_sma(x: np.ndarray, n: int) -> np.ndarray:
    """Average of the n values before t (t itself excluded)."""
    out = np.full(len(x), np.nan)
    out[1:] = sma(x, n)[:-1]
    return out


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    out[n] = np.nanmean(x[1 : n + 1])
    for i in range(n + 1, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def atr14(fr: Frame) -> np.ndarray:
    pc = np.r_[np.nan, fr.c[:-1]]
    tr = np.nanmax(np.vstack([fr.hi - fr.lo, np.abs(fr.hi - pc), np.abs(fr.lo - pc)]), axis=0)
    return wilder(tr, 14)


def rsi2(c: np.ndarray) -> np.ndarray:
    d = np.diff(c, prepend=np.nan)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    up[0] = dn[0] = np.nan
    au, ad = wilder(up, 2), wilder(dn, 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        r = 100 - 100 / (1 + rs)
    return np.where(ad == 0, 100.0, r)


_FRAMES: dict[str, Frame] | None = None


def frames() -> dict[str, Frame]:
    global _FRAMES
    if _FRAMES is None:
        raw = load_raw()
        nc = nifty_close()
        ok = nc > pd.Series(sma(nc.to_numpy(), 200), index=nc.index)
        _FRAMES = {s: Frame(s, f, ok) for s, f in raw.items()
                   if len(f) and f.index[0] < MIN_SPAN_START and s not in EXCLUDED}  # fmt: skip
    return _FRAMES


# ───────────────────────────── the setups (signal on close t, entry at open t + 1) ─────────────


def entries_m1(fr: Frame) -> np.ndarray:
    c = fr.c
    hi252 = pd.Series(c).rolling(252, min_periods=252).max().to_numpy()
    newhi = c >= hi252  # the highest close of 252 sessions, today included
    nh = pd.Series(newhi.astype(float))
    before = nh.shift(1).rolling(20, min_periods=20).max().to_numpy()  # any in t-20..t-1
    vol = fr.v >= 1.5 * prior_sma(fr.v, 50)
    return newhi & (before == 0) & vol & fr.mkt


def entries_m2(fr: Frame) -> np.ndarray:
    ph = np.r_[np.nan, fr.hi[:-1]]
    rng = fr.hi - fr.lo
    with np.errstate(invalid="ignore", divide="ignore"):
        upper = (fr.c - fr.lo) >= 0.5 * rng
    return (fr.o >= 1.03 * ph) & (fr.v >= 3 * prior_sma(fr.v, 50)) & upper & (rng > 0)


def entries_r1(fr: Frame) -> np.ndarray:
    return (rsi2(fr.c) < 10) & (fr.c > sma(fr.c, 200))


def loop(fr: Frame, ent: np.ndarray, exit_fn) -> list[tuple[int, int, int, float, bool]]:
    """The product's loop: a signal on the close of t enters at the open of t + 1; an exit
    signal on the close of u >= entry sells at the open of u + 1 (the last close when there is
    no later bar); the sale day's close is the first that may enter again."""
    n, out, t = fr.n, [], 0
    while t < n - 1:
        if not ent[t]:
            t += 1
            continue
        e = t + 1
        x, px, at_close = n - 1, fr.c[n - 1], True
        for u in range(e, n):
            if exit_fn(u, e, t):
                x, px, at_close = (u + 1, fr.o[u + 1], False) if u + 1 < n else (u, fr.c[u], True)
                break
        out.append((t, e, x, float(px), at_close))
        t = x
    return out


def trades_p1(fr: Frame, depth: float) -> list[tuple]:
    from onelot.delivery.rule import PullbackParams, simulate

    p = PullbackParams(depth=depth)
    out = []
    for r in simulate(fr.o, fr.c, fr.mkt, p):
        px = fr.c[r.exit] if r.at_close else fr.o[r.exit]
        out.append((r.signal, r.entry, r.exit, float(px), r.at_close))
    return out


def trades_m1a(fr: Frame) -> list[tuple]:
    s50, c = sma(fr.c, 50), fr.c
    return loop(fr, entries_m1(fr), lambda u, e, t: c[u] < s50[u] or u - e + 1 >= 60)


def trades_m1b(fr: Frame) -> list[tuple]:
    """The desk's ratchet on daily bars: first stop 2 x ATR(14) of the signal day under the
    fill, target 3 risks, extension on; levels raised by a bar hold from the next bar."""
    from onelot.manage import ratchet as R

    ent, atr, lc = entries_m1(fr), atr14(fr), legcost()
    prm = R.RatchetParams(extend=True)
    n, out, t = fr.n, [], 0
    while t < n - 1:
        if not ent[t] or not np.isfinite(atr[t]) or atr[t] <= 0:
            t += 1
            continue
        e = t + 1
        buy = fr.o[e] * (1 + SLIP)
        q = float(qty_for(fr.c[t : t + 1])[0])
        rt = (lc.leg(np.array([buy * q]), False)[0] + lc.leg(np.array([buy * q]), True)[0]
              + DP) / q + 2 * SLIP * buy  # fmt: skip
        plan = R.plan_from_risk(1, buy, 2.0 * atr[t], 3.0, rt)
        st = R.start(plan)
        x, px, at_close = n - 1, fr.c[n - 1], True
        for u in range(e, n):
            hit = R.exit_on_bar(plan, st, fr.o[u], fr.hi[u], fr.lo[u])
            if hit is not None:
                x, px, at_close = u, float(hit[1]), False
                break
            st, _ = R.advance(plan, st, fr.hi[u], fr.lo[u], prm)
        out.append((t, e, x, float(px), at_close))
        t = x
    return out


def trades_m2(fr: Frame, hold: int) -> list[tuple]:
    c, lo = fr.c, fr.lo
    return loop(fr, entries_m2(fr), lambda u, e, t: u - e + 1 >= hold or c[u] < lo[t])


def trades_r1(fr: Frame) -> list[tuple]:
    c, s5 = fr.c, sma(fr.c, 5)
    return loop(fr, entries_r1(fr), lambda u, e, t: c[u] > s5[u] or u - e + 1 >= 7)


def m3_ranks(frs: dict[str, Frame], day: pd.Timestamp) -> list[str]:
    """The top 10% by the return from 252 to 21 sessions before `day` (closes up to `day`)."""
    mom = {}
    for s, fr in frs.items():
        i = fr.dates.searchsorted(day)
        if i < len(fr.dates) and fr.dates[i] == day and i >= 252:
            mom[s] = fr.c[i - 21] / fr.c[i - 252] - 1
    if not mom:
        return []
    k = math.ceil(0.10 * len(mom))
    return [s for s, _ in sorted(mom.items(), key=lambda kv: -kv[1])[:k]]


def month_firsts(cal: pd.DatetimeIndex) -> list[pd.Timestamp]:
    s = pd.Series(cal, index=cal)
    return list(s.groupby([cal.year, cal.month]).min())


def trades_m3(frs: dict[str, Frame], nifty_ok: pd.Series) -> dict[str, list[tuple]]:
    """Rebalance on the close of each month's first session: hold the top 10% while NIFTY
    is above its SMA200 (else nothing); a stock still in the top 10% is kept, not resold."""
    firsts = month_firsts(nifty_ok.index)
    held: dict[str, tuple[int, int]] = {}
    out: dict[str, list[tuple]] = {s: [] for s in frs}
    for d in firsts:
        want = set(m3_ranks(frs, d)) if bool(nifty_ok.get(d, False)) else set()
        for s in list(held):
            if s in want:
                continue
            fr = frs[s]
            t, e = held.pop(s)
            i = fr.dates.searchsorted(d)  # the rebalance day or the stock's next bar
            if i + 1 < fr.n:
                out[s].append((t, e, i + 1, float(fr.o[i + 1]), False))
            else:
                out[s].append((t, e, fr.n - 1, float(fr.c[-1]), True))
        for s in want - set(held):
            fr = frs[s]
            t = int(fr.dates.searchsorted(d))
            if t + 1 < fr.n:
                held[s] = (t, t + 1)
    for s, (t, e) in held.items():
        fr = frs[s]
        out[s].append((t, e, fr.n - 1, float(fr.c[-1]), True))
    return out


SETUPS = {
    "P1": "pullback 8%, own exits", "P1b": "pullback 10%, own exits",
    "M1a": "new 52-week high, SMA50 or 60 sessions", "M1b": "new 52-week high, daily ratchet",
    "M2a": "gap up on volume, 20 sessions", "M2b": "gap up on volume, 40 sessions",
    "M3": "strongest 10%, monthly", "R1": "RSI(2) dip in an uptrend",
}  # fmt: skip
STAGE_SETUPS = {"p1": ("P1", "P1b"), "m1": ("M1a", "M1b"), "m2": ("M2a", "M2b"), "m3": ("M3",),
                "r1": ("R1",)}  # fmt: skip


def size_buckets(frs: dict[str, Frame]) -> dict[str, str]:
    """Three buckets by the median rupee turnover of the stock's TUNE bars."""
    med = {}
    for s, fr in frs.items():
        m = fr.dates <= pd.Timestamp("2024-11-07")
        med[s] = float(np.median((fr.c * fr.v)[m])) if m.any() else np.nan
    q = pd.Series(med).rank(pct=True)
    return {s: ("small" if r <= 1 / 3 else "mid" if r <= 2 / 3 else "large") for s, r in q.items()}


def to_frame(setup: str, rows: dict[str, list[tuple]], frs: dict[str, Frame]) -> pd.DataFrame:
    gmap, size = group_of(), size_buckets(frs)
    recs = []
    for s, lst in rows.items():
        fr = frs[s]
        for t, e, x, px, at_close in lst:
            recs.append((setup, s, gmap.get(s, "?"), size[s], t, e, x, fr.dates[t], fr.dates[e],
                         fr.dates[x], x - e, fr.o[e], px, int(qty_for(fr.c[t : t + 1])[0]),
                         bool(fr.mkt[t]), at_close))  # fmt: skip
    df = pd.DataFrame(recs, columns=["setup", "stock", "group", "size", "t", "e", "x", "signal_date",
                                     "entry_date", "exit_date", "days", "entry_open", "exit_raw",
                                     "qty", "nifty_up", "open_at_end"])  # fmt: skip
    df["net_pct"] = net_pct(df.entry_open.to_numpy(), df.exit_raw.to_numpy(), df.qty.to_numpy())
    df["amount"] = df.entry_open * (1 + SLIP) * df.qty
    df["net_inr"] = df.net_pct / 100 * df.amount
    df["bench_pct"] = bench(df, frs)
    df["excess"] = df.net_pct - df.bench_pct
    return df


_BENCH: dict[tuple[str, int], float] = {}


def bench(df: pd.DataFrame, frs: dict[str, Frame]) -> np.ndarray:
    """The same stock's average net % over the same number of sessions, open to open, from
    every start session with 252 bars behind it, same sizing and costs (survivorship check a)."""
    out = []
    for s, d in zip(df.stock, df.days, strict=True):
        d = max(1, int(d))
        key = (s, d)
        if key not in _BENCH:
            fr = frs[s]
            st = np.arange(252, fr.n - d)
            _BENCH[key] = (
                float(net_pct(fr.o[st], fr.o[st + d], qty_for(fr.c[st - 1])).mean())
                if len(st) else np.nan
            )  # fmt: skip
        out.append(_BENCH[key])
    return np.array(out)


def run_setups(names: tuple[str, ...]) -> None:
    frs = frames()
    for name in names:
        t0 = time.monotonic()
        if name == "M3":
            nc = nifty_close()
            ok = nc > pd.Series(sma(nc.to_numpy(), 200), index=nc.index)
            rows = trades_m3(frs, ok)
        else:
            fn = {"P1": lambda f: trades_p1(f, 0.08), "P1b": lambda f: trades_p1(f, 0.10),
                  "M1a": trades_m1a, "M1b": trades_m1b, "M2a": lambda f: trades_m2(f, 20),
                  "M2b": lambda f: trades_m2(f, 40), "R1": trades_r1}[name]  # fmt: skip
            rows = {s: fn(fr) for s, fr in frs.items()}
        df = to_frame(name, rows, frs)
        df.to_pickle(OUT / f"trades_{name}.pkl")
        print(f"{name}: {len(df)} trades, {df.net_pct.mean():+.2f}% a trade, "
              f"{time.monotonic() - t0:.0f}s", flush=True)  # fmt: skip


def stage_p1() -> None:
    run_setups(STAGE_SETUPS["p1"])


def stage_m1() -> None:
    run_setups(STAGE_SETUPS["m1"])


def stage_m2() -> None:
    run_setups(STAGE_SETUPS["m2"])


def stage_m3() -> None:
    run_setups(STAGE_SETUPS["m3"])


def stage_r1() -> None:
    run_setups(STAGE_SETUPS["r1"])


def all_trades() -> pd.DataFrame:
    return pd.concat([pd.read_pickle(OUT / f"trades_{k}.pkl") for k in SETUPS], ignore_index=True)


# ───────────────────────────── statistics and the selection rule ─────────────────────────────


def period_of(d: pd.Series) -> np.ndarray:
    return np.select([d <= pd.Timestamp("2024-11-07"), d <= pd.Timestamp("2025-11-07")],
                     ["TUNE", "CHOOSE"], "TEST")  # fmt: skip


def tweek(x: pd.Series, dates: pd.Series) -> float:
    """t of the mean with the trades of one ISO entry week summed into one draw."""
    if len(x) < 2:
        return np.nan
    iso = pd.DatetimeIndex(dates).isocalendar()
    w = pd.Series(np.asarray(x, float)).groupby([iso.year.values, iso.week.values]).sum()
    if len(w) < 2 or w.std(ddof=1) == 0:
        return np.nan
    return float(w.mean() / (w.std(ddof=1) / np.sqrt(len(w))))


def stats(g: pd.DataFrame) -> dict:
    if g.empty:
        return {"n": 0}
    x = g.net_pct
    win, loss = x[x > 0], x[x <= 0]
    per = {}
    for p in ("TUNE", "CHOOSE", "TEST"):
        q = g[g.period == p]
        per[p] = (len(q), float(q.net_pct.mean()) if len(q) else np.nan,
                  tweek(q.net_pct, q.entry_date), q.entry_date.nunique())  # fmt: skip
    return {
        "n": len(g), "days": g.entry_date.nunique(), "mean": float(x.mean()),
        "t": tweek(x, g.entry_date), "win": float((x > 0).mean() * 100),
        "payoff": float(win.mean() / -loss.mean()) if len(win) and len(loss) else np.nan,
        "exc": float(g.excess.mean()), "t_exc": tweek(g.excess, g.entry_date),
        "held": float(g.days.mean()), "per": per,
    }  # fmt: skip


def classify(st: dict) -> tuple[bool, bool, bool, bool]:
    """(examined, discovered, confirmed, passed) by RESEARCH-RULES section 5, delivery."""
    if st.get("n", 0) == 0:
        return False, False, False, False
    tn, tm, tt, td = st["per"]["TUNE"]
    examined = tn >= 60 and td >= 30
    disc = examined and tm > 0 and np.nan_to_num(tt) >= 2.5 and tm >= 0.50
    cn, cm, ct, _ = st["per"]["CHOOSE"]
    conf = disc and cn >= 20 and cm > 0 and np.nan_to_num(ct) >= 1.0
    sn, sm, _, _ = st["per"]["TEST"]
    pas = conf and sn >= 20 and sm > 0
    return examined, disc, conf, pas


CONDS = ("all", "group", "size", "nifty")
FILTERED = ("P1", "P1b", "M1a", "M1b", "M3")


def cells(tr: pd.DataFrame) -> list[tuple[str, str, str, pd.DataFrame]]:
    out = []
    for s in SETUPS:
        g = tr[tr.setup == s]
        out.append((s, "all", "all", g))
        for c in CONDS[1:]:
            if c == "nifty" and s in FILTERED:
                continue  # NIFTY above its SMA200 is part of the setup: the cell is "all"
            for v in sorted(tr[c].unique(), key=str):
                out.append((s, c, str(v), g[g[c] == v]))
    return out


def select_counts(tr: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    rows = []
    for s, c, v, g in cells(tr):
        st = stats(g)
        e, d, cf, p = classify(st)
        rows.append({"setup": s, "cond": c, "value": v, "n": st.get("n", 0),
                     "examined": e, "discovered": d, "confirmed": cf, "passed": p,
                     "stats": st})  # fmt: skip
    df = pd.DataFrame(rows)
    cnt = {k: int(df[k].sum()) for k in ("examined", "discovered", "confirmed", "passed")}
    return df, cnt


def with_period(tr: pd.DataFrame) -> pd.DataFrame:
    tr = tr.copy()
    tr["period"] = period_of(tr.entry_date)
    tr["nifty"] = np.where(tr.nifty_up, "up", "down")
    return tr


def stage_select() -> None:
    tr = with_period(all_trades())
    df, cnt = select_counts(tr)
    luck = cnt["examined"] * 0.006 * 0.16 * 0.5
    rng = np.random.default_rng(20261124)
    # placebo of labels: NIFTY's state is a day label, shuffled over the days inside each stock
    # and calendar year (whole days keep their trades); group and size are labels of a STOCK,
    # so they are shuffled over the stocks (a day shuffle cannot move them)
    plc = []
    stocks = sorted(tr.stock.unique())
    smap = tr.drop_duplicates("stock").set_index("stock")[["group", "size"]]
    for _ in range(20):
        sh = tr.copy()
        perm = rng.permutation(stocks)
        sh["group"] = sh.stock.map(dict(zip(stocks, smap.loc[perm, "group"], strict=True)))
        sh["size"] = sh.stock.map(dict(zip(stocks, smap.loc[perm, "size"], strict=True)))
        yr = sh.signal_date.dt.year
        lab = {}
        for (s, _y), g in sh.groupby([sh.stock, yr]):
            days = g.signal_date.unique()
            vals = g.drop_duplicates("signal_date").nifty.to_numpy()
            lab.update({(s, d): v for d, v in zip(days, rng.permutation(vals), strict=True)})
        sh["nifty"] = [lab[(s, d)] for s, d in zip(sh.stock, sh.signal_date, strict=True)]
        d2, _ = select_counts(sh)
        d2 = d2[d2.cond != "all"]
        plc.append({k: int(d2[k].sum()) for k in ("examined", "discovered", "confirmed", "passed")})
    plc_mean = {k: float(np.mean([p[k] for p in plc])) for k in plc[0]}
    real_cond = df[df.cond != "all"]
    cnt_cond = {
        k: int(real_cond[k].sum()) for k in ("examined", "discovered", "confirmed", "passed")
    }
    df.drop(columns="stats").to_csv(OUT / "cells.csv", index=False)
    pd.to_pickle(df, OUT / "cells.pkl")
    res = {"counts": cnt, "luck_arith": luck, "cond_counts": cnt_cond, "placebo20": plc_mean}
    (OUT / "select.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    print(df[df.examined][["setup", "cond", "value", "n", "discovered", "confirmed", "passed"]]
          .to_string())  # fmt: skip


# ───────────────────────────── placebo of entry dates (survivorship check b) ─────────────────


def placebo_runs(g: pd.DataFrame, frs: dict[str, Frame], runs: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = np.zeros(runs)
    by_key: dict[tuple[str, int], np.ndarray] = {}
    for s, fr in frs.items():
        yrs = fr.dates.year
        for y in np.unique(yrs):
            by_key[(s, int(y))] = np.flatnonzero((yrs == y) & (np.arange(fr.n) >= 252))
    parts = []
    for (s, y, d), q in g.groupby([g.stock, g.entry_date.dt.year, g.days.clip(lower=1)]):
        fr = frs[s]
        cand = by_key.get((s, int(y)), np.array([], int))
        cand = cand[cand + d < fr.n]
        if len(cand) == 0:
            continue
        idx = rng.choice(cand, size=(runs, len(q)))
        r = net_pct(fr.o[idx], fr.o[idx + d], qty_for(fr.c[idx - 1]))
        parts.append(r)
    allr = np.concatenate(parts, axis=1)
    out = allr.mean(axis=1)
    return out


def stage_placebo() -> None:
    tr = with_period(all_trades())
    frs = frames()
    res = {}
    for s in SETUPS:
        for lab, g in (("all", tr[tr.setup == s]), ("new", tr[(tr.setup == s) & (tr.group == "new")]),
                       ("alpha_C", tr[(tr.setup == s) & tr.stock.isin(ALPHA_C)])):  # fmt: skip
            if g.empty:
                continue
            runs = placebo_runs(g, frs, 200, seed=hash((s, lab)) % 2**32)
            real = float(g.net_pct.mean())
            res[f"{s}|{lab}"] = {"real": real, "placebo_mean": float(runs.mean()),
                                 "p05": float(np.percentile(runs, 5)),
                                 "p95": float(np.percentile(runs, 95)),
                                 "share_ge_real": float((runs >= real).mean())}  # fmt: skip
            print(s, lab, json.dumps(res[f"{s}|{lab}"]), flush=True)
    (OUT / "placebo.json").write_text(json.dumps(res, indent=1))


# ───────────────────────────── look-ahead: the truncation test ─────────────────────────────


def stage_lookahead() -> None:
    """Cut the bars (the stock's and NIFTY's) after a signal bar and read the signal again on
    the cut data: it must not change. 60 random signals and 20 random non-signals a setup."""
    from onelot.delivery.rule import PullbackParams, entry_reading

    raw, nc = load_raw(), nifty_close()
    tr = all_trades()
    rng = np.random.default_rng(7)
    L = []

    def cut(s: str, day: pd.Timestamp) -> Frame:
        n2 = nc[nc.index <= day]
        ok = n2 > pd.Series(sma(n2.to_numpy(), 200), index=n2.index)
        return Frame(s, raw[s][raw[s].index <= day], ok)

    def read(setup: str, fr: Frame) -> bool:
        t = fr.n - 1
        if setup in ("P1", "P1b"):
            p = PullbackParams(depth=0.08 if setup == "P1" else 0.10)
            return entry_reading(fr.c, fr.mkt, t, p).signal
        if setup in ("M1a", "M1b"):
            return bool(entries_m1(fr)[t])
        if setup in ("M2a", "M2b"):
            return bool(entries_m2(fr)[t])
        return bool(entries_r1(fr)[t])

    for setup in [k for k in SETUPS if k != "M3"]:
        g = tr[tr.setup == setup]
        pick = g.iloc[rng.choice(len(g), size=min(60, len(g)), replace=False)]
        same = sum(read(setup, cut(r.stock, r.signal_date)) for r in pick.itertuples())
        neg, k = 0, 0
        stocks = sorted(g.stock.unique())
        while k < 20:
            s = stocks[rng.integers(len(stocks))]
            f = raw[s]
            d = f.index[rng.integers(300, len(f) - 1)]
            if ((g.stock == s) & (g.signal_date == d)).any():
                continue
            full = frames()[s]
            i = full.dates.get_loc(d)
            want = read_full(setup, full, i)
            neg += int(read(setup, cut(s, d)) == want)
            k += 1
        L.append(f"{setup}: {same} of {len(pick)} signals unchanged on cut data; "
                 f"{neg} of 20 other bars read the same")  # fmt: skip
    g = tr[tr.setup == "M3"]
    days = sorted(g.signal_date.unique())
    pick = [days[i] for i in rng.choice(len(days), size=min(30, len(days)), replace=False)]
    ok_all, n_sig = 0, 0
    frs = frames()
    for d in pick:
        d = pd.Timestamp(d)
        full = set(m3_ranks(frs, d))
        cutf = {s: cut(s, d) for s in frs if raw[s].index[0] <= d}
        ok_all += int(set(m3_ranks(cutf, d)) == full)
        n_sig += 1
    L.append(f"M3: the top 10% of {ok_all} of {n_sig} random rebalance days unchanged on cut data")
    (OUT / "lookahead.txt").write_text("\n".join(L) + "\n")
    print("\n".join(L))


def read_full(setup: str, fr: Frame, i: int) -> bool:
    from onelot.delivery.rule import PullbackParams, entry_reading

    if setup in ("P1", "P1b"):
        p = PullbackParams(depth=0.08 if setup == "P1" else 0.10)
        return entry_reading(fr.c, fr.mkt, i, p).signal if i >= p.min_bars - 1 else False
    if setup in ("M1a", "M1b"):
        return bool(entries_m1(fr)[i])
    if setup in ("M2a", "M2b"):
        return bool(entries_m2(fr)[i])
    return bool(entries_r1(fr)[i])


# ───────────────────────────── the report ─────────────────────────────


def fnum(x: float, d: int = 2, sign: bool = True) -> str:
    if x is None or not np.isfinite(x):
        return "n/a"
    s = f"{x:+.{d}f}" if sign else f"{x:.{d}f}"
    return s.replace("-", "−")


def row(st: dict) -> str:
    if st.get("n", 0) == 0:
        return "0 trades"
    per = " / ".join(
        f"{fnum(st['per'][p][1])} ({st['per'][p][0]})" for p in ("TUNE", "CHOOSE", "TEST")
    )
    return (f"{st['n']} | {st['days']} | {fnum(st['mean'])} (t {fnum(st['t'], 1)}) | {st['win']:.0f}% | "
            f"{fnum(st['payoff'], 2, False)} | {st['held']:.0f} | {per} | "
            f"{fnum(st['exc'])} (t {fnum(st['t_exc'], 1)})")  # fmt: skip


def owner_view(g: pd.DataFrame) -> dict:
    cal = pd.DatetimeIndex(sorted(set().union(*[set(f.dates) for f in frames().values()])))
    cal = cal[(cal >= g.entry_date.min()) & (cal <= LAST)]
    e, x = g.entry_date.to_numpy(), g.exit_date.to_numpy()
    openn = np.array([int(((e <= d) & (x > d)).sum()) for d in cal.to_numpy()])
    months = max(1.0, (LAST - g.entry_date.min()).days / 30.44)
    seq = g.sort_values("exit_date").net_pct.to_numpy() <= 0
    run = best = 0
    for lose in seq:
        run = run + 1 if lose else 0
        best = max(best, run)
    mon = g.groupby(g.exit_date.dt.to_period("M")).net_inr.sum()
    yr = g.groupby(g.exit_date.dt.year).net_inr.sum()
    return {"per_month": len(g) / months, "open_median": float(np.median(openn)),
            "open_max": int(openn.max()), "lose_run": best, "worst_month": float(mon.min()),
            "worst_month_at": str(mon.idxmin()), "worst_year": float(yr.min()),
            "worst_year_at": int(yr.idxmin()), "total": float(g.net_inr.sum())}  # fmt: skip


def stage_report() -> None:
    tr = with_period(all_trades())
    frs = frames()
    cells_df = pd.read_pickle(OUT / "cells.pkl")
    sel = json.loads((OUT / "select.json").read_text())
    plc = json.loads((OUT / "placebo.json").read_text())
    L = []
    gm = group_of()
    kept = sorted(frs)
    L.append(f"UNIVERSE: {len(kept)} stocks; " + str(pd.Series([gm[s] for s in kept]).value_counts()
                                                    .to_dict()))  # fmt: skip
    first = pd.Series({s: frs[s].dates[0].year for s in kept})
    L.append(f"first bar 2010: {(first == 2010).sum()}, later: {(first > 2010).sum()} "
             f"(latest {first.max()})")  # fmt: skip
    last = {s: frs[s].c[-1] for s in kept}
    dear = sorted((s for s, c in last.items() if c > UNIT), key=lambda s: -last[s])
    L.append(
        f"priced over Rs 10,000 a share on {LAST.date()}: {len(dear)}: "
        + ", ".join(f"{s} {last[s]:,.0f}" for s in dear)
    )
    amt = tr.amount
    L.append(f"trade amount: median Rs {amt.median():,.0f}, 5th pct Rs {amt.quantile(0.05):,.0f}, "
             f"share of trades under Rs 9,000: {(amt < 9000).mean():.1%}")  # fmt: skip
    L.append(
        f"round trip at Rs 10,000 (charges, DP, slippage): "
        f"{fnum(-float(net_pct(np.array([1000.0]), np.array([1000.0]), np.array([10]))[0]))}%"
    )
    L.append("")
    hdr = ("trades | entry days | % a trade (t) | win | payoff | held | TUNE / CHOOSE / TEST (n) | "
           "excess over same-stock hold (t)")  # fmt: skip
    for s in SETUPS:
        g = tr[tr.setup == s]
        L.append(f"== {s}: {SETUPS[s]}")
        L.append("   " + hdr)
        L.append("   all     " + row(stats(g)))
        for lab, q in (("desk18", g[g.group == "desk18"]), ("alpha43", g[g.group == "alpha43"]),
                       ("new", g[g.group == "new"]), ("alpha C", g[g.stock.isin(ALPHA_C)])):  # fmt: skip
            L.append(f"   {lab:7s} " + row(stats(q)))
        for c in ("nifty", "size"):
            for v in sorted(g[c].unique()):
                L.append(f"   {c}={v:5s} " + row(stats(g[g[c] == v])))
        for lab in ("all", "new", "alpha_C"):
            p = plc.get(f"{s}|{lab}")
            if p:
                L.append(f"   placebo {lab}: real {fnum(p['real'])}, random dates "
                         f"{fnum(p['placebo_mean'])} [{fnum(p['p05'])}, {fnum(p['p95'])}], "
                         f"share of runs at or above real {p['share_ge_real']:.3f}")  # fmt: skip
        yr = g.groupby(g.entry_date.dt.year).net_pct.agg(["mean", "size"])
        L.append(
            "   by year: " + " ".join(f"{y} {fnum(m)}({int(n)})" for y, (m, n) in yr.iterrows())
        )
        yrn = g[g.group == "new"].groupby(g.entry_date.dt.year).net_pct.agg(["mean", "size"])
        L.append(
            "   new by year: "
            + " ".join(f"{y} {fnum(m)}({int(n)})" for y, (m, n) in yrn.iterrows())
        )
        neg_years = int((yr["mean"] < 0).sum())
        L.append(f"   years below zero: {neg_years} of {len(yr)}; trades a year "
                 f"{len(g) / ((LAST - g.entry_date.min()).days / 365.25):.0f}")  # fmt: skip
        ov = owner_view(g)
        L.append(f"   book of every signal at Rs 10,000: {ov['per_month']:.1f} signals a month, "
                 f"open at once median {ov['open_median']:.0f} worst {ov['open_max']}, longest "
                 f"losing run {ov['lose_run']}, worst month Rs {ov['worst_month']:,.0f} "
                 f"({ov['worst_month_at']}), worst year Rs {ov['worst_year']:,.0f} "
                 f"({ov['worst_year_at']}), total Rs {ov['total']:,.0f}")  # fmt: skip
        tops = g.groupby("stock").net_pct.agg(["mean", "size"])
        tops = tops[tops["size"] >= 10].sort_values("mean")
        L.append(
            "   strongest stocks (10+ trades): "
            + ", ".join(f"{k} {fnum(m)}({int(n)})" for k, (m, n) in tops.tail(5)[::-1].iterrows())
        )
        L.append(
            "   weakest stocks (10+ trades): "
            + ", ".join(f"{k} {fnum(m)}({int(n)})" for k, (m, n) in tops.head(5).iterrows())
        )
        L.append(f"   stocks above zero: {(tops['mean'] > 0).sum()} of {len(tops)}")
        L.append("")
    L.append("SELECTION (RESEARCH-RULES 5, delivery thresholds): " + json.dumps(sel))
    ex = cells_df[cells_df.examined]
    for r in ex.itertuples():
        st = r.stats
        tn, tm, tt, _ = st["per"]["TUNE"]
        cn, cm, ct, _ = st["per"]["CHOOSE"]
        sn, sm, _, _ = st["per"]["TEST"]
        flag = ("PASSED" if r.passed else "confirmed" if r.confirmed else
                "discovered" if r.discovered else "")  # fmt: skip
        L.append(f"   {r.setup:4s} {r.cond:6s} {r.value:8s} TUNE {fnum(tm)} t {fnum(tt, 1)} ({tn}) "
                 f"CHOOSE {fnum(cm)} t {fnum(ct, 1)} ({cn}) TEST {fnum(sm)} ({sn}) {flag}")  # fmt: skip
    not_ex = cells_df[~cells_df.examined & (cells_df.n > 0)]
    L.append(
        "   not examined (TUNE under 60 trades or 30 days): "
        + ", ".join(f"{r.setup} {r.cond}={r.value} ({r.n})" for r in not_ex.itertuples())
    )
    (OUT / "report.txt").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "report"
    globals()[f"stage_{stage}"]()
