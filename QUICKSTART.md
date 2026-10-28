# Quick start

```bash
scripts/start_mac.sh --mock        # macOS/Linux — synthetic market, no credentials
scripts\start_win.bat --mock       # Windows
```

Open http://127.0.0.1:8787/ (add `--lan` to reach it from your phone or another laptop).
Bright **Day** theme by default; **☾ Night** in the header switches.

1. **Settings** → paste Dhan client ID + free-tier token → Save → **Test entitlements**.
2. **Home** → your funds, holdings (marked at Dhan's last price), positions, 30-day ledger and
   trade book (cached; press Refresh). All of this works on the free tier.
3. **Backtest Lab** → OPTIONS · NIFTY BANK options · EMA Cross · 12m → **Run backtest**.
   Then **Walk-forward**. Only out-of-sample numbers count.
4. **Data** → coverage of the local corpus; backfill from Yahoo now, from Dhan (1-minute, 5 years) once the Data API is active.
5. **News & Sentiment** → post scores manually or from your scorer (`POST /api/sentiment`).
6. **Live Desk** → 5 indices + 10 stocks under several strategies each, on delayed Yahoo bars until the
   Data API is active (then 1-minute Dhan bars, automatically). Expand a row for market / regime /
   risk / data health. Take or skip alerts; close what you took.

Shell:

```bash
.venv/bin/python -m onelot preflight
.venv/bin/python -m onelot backfill
.venv/bin/python -m onelot backtest --strategy supertrend_vol --instrument BANKNIFTY_FUT --timeframe 5m
.venv/bin/python -m onelot research --instruments BANKNIFTY_OPT --timeframes 5m,15m
.venv/bin/python -m onelot sweep --strategies all --instruments focus --timeframes 5m,15m,30m,60m
.venv/bin/python -m onelot leaderboard --min-trades 10
.venv/bin/python -m pytest -q
```

Stop with `scripts/stop_mac.sh` / `scripts\stop_win.bat` or Ctrl-C.
