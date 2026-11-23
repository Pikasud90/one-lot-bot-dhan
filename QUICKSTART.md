# Quick start

One Lot Desk is signal-only: it never places an order. You need Python 3.10 or newer and git.
Every command below was run on a fresh clone.

## 1. Start it on the synthetic market (no credentials, no history)

```bash
git clone https://github.com/Pikasud90/one-lot-bot-dhan.git onelot
cd onelot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m onelot run --mock --port 8787
```

On Windows use `.venv\Scripts\python` in place of `.venv/bin/python`. The first start takes
about half a minute before the page answers. Then open http://127.0.0.1:8787/ . Stop it with
Ctrl-C.

The launcher scripts do the environment and install steps for you on the first run:

```bash
scripts/start_mac.sh --mock        # macOS
scripts/start_linux.sh --mock      # Linux
scripts\start_win.bat --mock       # Windows
```

Add `--port N` to change the port and `--lan` to reach the desk from your phone or another
laptop on your network.

## 2. What works without market history

A fresh clone has no market history: it is not part of the repository. Home, Live Desk, Data,
News and Settings work without it. These tabs need it:

* Backtest Lab
* Simulator
* Machine Learning Lab (60m and 4h)

Download it (free, from Yahoo) while the desk is stopped or running:

```bash
.venv/bin/python -m onelot backfill
.venv/bin/python -m onelot quality
```

The full download takes a long while. One series is quick, for a first try:
`backfill --series BANKNIFTY --resolutions 1d`.

## 3. Real data

Either paste your Dhan client id and access token in the Settings tab, or copy the example
file and fill it in (names only in the example, never commit your own):

```bash
cp .env.example .env
.venv/bin/python -m onelot preflight
```

`preflight` says in plain words what is missing: history, credentials, and which Dhan
endpoints your token can reach. Then start without `--mock`:

```bash
.venv/bin/python -m onelot run --port 8787
```

## 4. Stop and update

```bash
scripts/stop_mac.sh                # macOS and Linux (scripts\stop_win.bat on Windows)
git pull
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.lock.txt` pins the exact versions this desk was tested with. Install from it
instead of `requirements.txt` when you want that exact environment.

## 5. In a container

```bash
docker build -t onelot-desk .
docker compose up --build          # http://127.0.0.1:8787/ ; data and logs stay in ./data, ./logs
```

Put credentials in `.env` before `docker compose up`; the file is optional. These two
commands have not been run end to end yet: the machine that wrote this guide had no running
Docker daemon. Report anything that fails.

More commands (backtest, research, sweep, leaderboard) are in README.md.
