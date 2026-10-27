# Disclaimer

**This software is a trade-support tool. It is not investment advice.**

- It **places no orders**. It contains no order-placement code path, by design and by test.
  Every trade is decided and executed by you, manually, on your own broker terminal.
- Nothing it displays is a prediction, a recommendation, or a guarantee of profit.
- The strategy parameters shipped in `config/config.yaml` are **arbitrary starting points**
  chosen for illustration. They have not been validated against your capital, your risk
  tolerance, or any live market. They require calibration before they mean anything.
- Buying options carries the risk of losing **100% of the premium paid**. Leverage magnifies
  losses. Intraday trading can lose money faster than any stop-loss can react, particularly
  during gaps, circuit halts, and illiquid conditions.
- The statutory charges in `config/costs.yaml` are the author's best understanding at the
  stated `effective_from` date. **Rates change.** You are responsible for reconciling them
  against a real contract note from your broker before relying on any net-P&L figure. The
  file ships with `verified_against_contract_note: false` and the engine warns until you
  change it.
- Market data may be delayed, wrong, or absent. Broker APIs fail. Your network fails. The
  software degrades loudly rather than silently, but it cannot make a dead feed live.
- You are solely responsible for your trades, your taxes, and your compliance with all
  applicable regulations.

**Run in `PAPER` mode until you have forward-tested long enough to have your own evidence.**
`PAPER` is the default and switching to `LIVE` requires an explicit, deliberate change.
