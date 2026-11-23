"""The Simulator (US-075): trade a strategy's alerts on past data, or on the live feed, with
dummy money and market-accurate fills. Nothing here can place an order; simulated trades live
in their own `sim_*` tables and never reach the paper test.

    bars.py     where the one-minute bars come from (the store, or the running desk)
    pricing.py  the traded price (share or option premium) and the charges
    engine.py   one session: cursor, signals, alert, trade, the ratchet
    store.py    the `sim_*` tables
    manager.py  sessions by id, rebuilt from the tables when not in memory
    routes.py   the `/api/sim/*` routes (`.agile/stories/SIM-API.md`)
"""
