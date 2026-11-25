/* Cheat sheets: one block per tab. Content lives only between the two marker lines. */
// CHEATS-DATA-BEGIN
const CHEATS = {
  "home": {
    what: [
      "Home is the day's record. It holds the Today strip, the alerts the desk raised, the paper trade each one opened, a scoreboard, the delivery list and your own journal.",
      "The desk only alerts and keeps paper trades. It never places an order, and a paper trade is not a real fill.",
      "Alerts, paper trades and the scoreboard come from the desk's own database, built on Dhan one-minute bars or, when that feed is down, Yahoo bars about 15 minutes late. The portfolio tiles are read from the broker and no rule uses them.",
    ],
    read: [
      { term: "Feed", meaning: "On the Today strip. 'live (dhan)' in green means Dhan bars. 'delayed ~15 min' in amber means Yahoo bars. Red means the source last reported an error. Option alerts need clean live one-minute bars." },
      { term: "Dhan token", meaning: "Time left on the broker token, read from the cached profile. Green above 2 hours, red under 2 hours or EXPIRED. The 'Next' line then says to renew it in Settings. The header chip turns amber under 2 hours." },
      { term: "53 medium", meaning: "The confidence badge on an option alert: a score from 0 to 100. 65 or more reads high, 45 up to 65 reads medium, below 45 reads low. The page shows the score cut down to a whole number. Hover to see the parts." },
      { term: "signal strength · backtest evidence · live record · regime fit", meaning: "The four parts, each 0 to 100, weighted 35%, 30%, 20% and 15%. A part with no evidence is 50. Regime fit is 70 if the regime is allowed, 30 if not, 50 if unknown. The micro-trend rule has no strength score, so its 'strength 50' is fixed." },
      { term: "backtest evidence", meaning: "Taken from the saved Lab run for this strategy, instrument and timeframe (a walk-forward run is preferred). It is 50 plus 100 times average R a trade (limited to plus or minus 0.5), minus 15 if profit factor is under 1. It moves toward 50 until 40 trades." },
      { term: "live record", meaning: "Needs 5 or more closed audited alerts of the strategy (all instruments together). It is 50 plus 0.6 times the percent positive plus 50 times the average R, kept between 0 and 100. Below 5 alerts it is 50." },
      { term: "PENDING · TAKEN · EXPIRED · SKIPPED", meaning: "PENDING: waiting for you, valid 10 minutes for an option alert. TAKEN: you pressed Trade taken. SKIPPED: you pressed Skipped. EXPIRED: no answer in time. Every alert opens a paper trade whatever you press." },
      { term: "EXIT … / NO FILL", meaning: "The alert's one exit is its paper trade closing, with a reason: STOP, BREAKEVEN STOP, LOCK STOP, TRAIL STOP, TARGET, SIGNAL EXIT or SESSION END. NO FILL means the paper trade never filled, for example it gapped through its stop." },
      { term: "Audit at the alerted levels", meaning: "Replays the alert's own entry, stop and target on the index bars that followed. Entry is taken at the signal bar's close and a tie goes to the stop. 'Best / worst excursion' is in R. 'Est. P&L (1 unit)' is a model estimate." },
      { term: "Strategy scoreboard", meaning: "One row per strategy, timeframe and market. Win % is the share of paper trades above zero after costs. Net ₹ is after costs. Profit factor is rupees won over rupees lost. Avg R is net result over the first risk. No fill never filled." },
      { term: "Left out of the score", meaning: "Trades on contracts with no open interest when the alert came. They stay listed with the tag 'not counted' and are in no score. The line says how many and their net." },
      { term: "Yours vs the alerts'", meaning: "In the Desk journal. The alert's prices are the record. Your own entry and exit, if you typed them under 'my prices', sit beside them with the rupee difference." },
      { term: "Delivery (pullback)", meaning: "The stock rule. After the close: close above SMA200, NIFTY above its SMA200, first close 8% or more under the 20-session closing high. Exit at the first close above SMA20, after 30 sessions, or 15% below entry." },
      { term: "Est. premium / premium synthetic (Black-76)", meaning: "The option price on the alert. It comes from the live chain's ask when a tradeable contract exists. 'Synthetic' means a model price from India VIX, used when no tradeable contract was found." },
    ],
    use: [
      "Before 09:15, read the Today strip: session, feed, Dhan token and the Next line. If the token has under two hours left, renew it in Settings.",
      "During the session a pending option alert shows as a card. Open it and read the levels, the confidence parts and the data tag (live or delayed). The paper trade is already open.",
      "Press Trade taken only if you placed the trade in Dhan yourself, and type your own fill price so the journal can show the gap.",
      "After the close the pullback alerts for stocks arrive in the evening, about ten minutes after the close. Read the Archive, the scoreboard and the Desk journal then.",
      "Read confidence as a note on how much evidence sits behind the rule. Read the scoreboard only when it has many trades.",
    ],
    trust: [
      { level: "real", text: "Alert times, signal bars, the data source tag, Trade taken or Skipped, and the prices you type are recorded facts." },
      { level: "estimate", text: "Confidence is a score built from the strategy's own past record and the signal's strength. It is NOT a probability of profit and NOT a machine-learning forecast." },
      { level: "estimate", text: "For the micro-trend rule strength is always 50 and regime fit is 70 for any known regime, so 53 medium means no evidence. Only backtest evidence and live record can move it, between about 24 and 78." },
      { level: "estimate", text: "A paper trade fills at the open of the first bar after the alert with modelled spread and charges. An option leg uses the contract's traded one-minute price when found, else a Black-76 model price. The page does not say which." },
      { level: "estimate", text: "Cost rates are not yet checked against a real Dhan contract note (the app warns at start), so every Net is an estimate. The model's 0.6% option half spread is above the measured 0.25% to 0.3% on NIFTY and BANKNIFTY and below 1.5% on FINNIFTY and SENSEX." },
      { level: "estimate", text: "Audit rupee figures for options re-price the contract with a model, so they can differ from the paper trade's own result for the same alert." },
      { level: "gap", text: "The record is tiny and shows no edge. At the 24 Nov 2026 study the micro-trend rule had raised 2 live alerts. Its backtest lost about 0.2% of premium a trade on modelled prices and about 1.2% on real ones." },
      { level: "gap", text: "The pullback rule as a ten-trade book returned less than NIFTY held. Account tiles are read from the broker, cached, refreshed only on request or after a trade, and used by no rule." },
    ],
    dont: [
      "Do not read high or medium confidence as a chance of winning. It is a bookkeeping score and for the micro-trend rule it is mostly fixed.",
      "Do not read a short scoreboard as proof. Under about 30 trades every figure is noise.",
      "Do not read a paper result as a real fill. Real fills can be worse, and the alert is valid only 10 minutes.",
      "Do not compare the audit and the paper result as one trade. They use different entry points, prices and win definitions.",
    ],
  },
  "live": {
    what: [
      "The Live Desk is the working view of the two rules. The micro-trend rule watches four indices for option alerts (one lot, same day). The pullback rule watches 18 stocks for delivery trades of about Rs 10,000.",
      "It shows open positions, pending alerts, the paper trades, measured fill costs, market context and one card per instrument. Alerts and paper trades only: the desk never places an order.",
      "Numbers come from Dhan one-minute bars and quotes when the Data API plan is active. Otherwise they come from Yahoo bars about 15 minutes late, and the page says so.",
    ],
    read: [
      { term: "desk dhan 1m · live", meaning: "The line under Market context: bar source, bar size, 'live' or 'delayed ~15 min', poll seconds, market open or closed, entries allowed or not, and the last update. A red warning shows the source's last error." },
      { term: "Open positions", meaning: "Alerts you took, one lot of index options. Alert entry is the alert's price, Your entry is what you typed. R now and Unrealised are measured at the last index level. Exit says hold, 'on its exit alert' or EXIT with a reason." },
      { term: "valid 9:41 / expired", meaning: "The countdown on an alert card. An option alert lasts 10 minutes. It turns urgent under one minute. After that the alert is EXPIRED, but its paper trade still runs." },
      { term: "Est. premium · Premium at stop · Premium at target", meaning: "Option prices for one lot at the alert, the stop and the target. 'trail' replaces the target when the stop and trail rule manages it. Risk ₹ includes charges. Net R/R is net reward over net risk. Outlay is the money to buy." },
      { term: "premium synthetic", meaning: "A model price (Black-76 from India VIX) was used because no tradeable contract was found in the live chain. Check the real price on your Dhan screen before acting." },
      { term: "pending · open · no fill · closed", meaning: "Chips in Paper trades. pending waits for its fill bar. open is filled. no fill never filled. closed has an exit. Grade 'live' means Dhan bars served it, 'delayed' means Yahoo bars." },
      { term: "Gross (trade) / Net (trade)", meaning: "The whole trade in rupees before and after costs: (exit minus entry) times quantity for a buy. Net shows a dash until costs are applied. Today, Last session and All choose the list." },
      { term: "not counted", meaning: "A paper trade on a contract with no open interest. It is listed with the tag and left out of every total. The day line then says 'Left out of the score: N trades on contracts that were not tradeable'." },
      { term: "Micro-trend strip ▲ ▼ ·", meaning: "Supertrend(14, 1.7) on 3m (the trigger), 5m, 10m, 15m, 30m, 60m and 4h. ▲ up, ▼ down, · no reading. An option alert needs all seven to newly agree. 'reversed k of 7, exit at 3' marks an open trade." },
      { term: "TRACKING · BUY · SELL · HOLD", meaning: "The badge on each instrument row. TRACKING means no pending alert. BUY or SELL means an option alert is waiting for you. HOLD means you took a trade there. MIDCPNIFTY is tagged 'Training only: no alerts'." },
      { term: "pullback 1d · 6.1% of 8%", meaning: "How far the stock closed under its 20-session closing high (6.1%) against the 8% needed. 'BUY at the next open' appears when the whole rule is met. 'open 3 of 30' counts sessions held." },
      { term: "OK · LAGGING · STALE · CLOSED · NO DATA", meaning: "Data health of an instrument. With one-minute live bars: LAGGING when the last bar is over about 2.5 minutes old, STALE over about 7.5 minutes. CLOSED is outside the session. NO DATA means no bars yet." },
      { term: "conf 53", meaning: "In Instrument focus, under 'Pending alert'. The same confidence score as on Home: a bookkeeping score from strength, backtest, live record and regime fit, not a probability. See the Home sheet for the parts." },
      { term: "Half-spread (median) / Fill vs alert / +1 min", meaning: "Fill costs: measured bid and ask quotes every 5 minutes and at each alert. Half-spread is the cost of one side. 'Fill vs alert' and '+1, +2, +5 min' show drift after the alert. Positive means a worse price." },
    ],
    use: [
      "Before 09:15 check the source line: Dhan, 'live', poll 60s, and each row's Data health OK or CLOSED. If it says delayed, the micro-trend rule raises no alert.",
      "During the session scan the micro-trend strips. An option alert comes only when all seven trends newly agree on a 3-minute close, so quiet hours are normal.",
      "When a card appears you have 10 minutes. Read Est. premium, stop, target, Risk ₹ and Net R/R, then check the contract on Dhan. Press Trade taken only after you really traded.",
      "While a trade is open, watch Exit. When the stop or target moves you get a message to move your own stop. The desk never changes an order.",
      "After the close read Paper trades (Last session) and Fill costs. The pullback alerts for stocks come in the evening.",
    ],
    trust: [
      { level: "real", text: "One-minute index and stock bars, quotes and the bid and ask in Fill costs are measured Dhan data. Times are IST. The paper trade and your typed fills are recorded facts." },
      { level: "real", text: "Fill costs come from real quotes of the contract an alert would name. They show what one side costs and are the best check on the model's assumed cost." },
      { level: "estimate", text: "Est. premium is the live chain's ask for the tradeable contract near delta 0.65, or a Black-76 model price from India VIX when marked synthetic." },
      { level: "estimate", text: "Unrealised and R now on an option are measured on a model premium for the last index level, not on the contract's quote. They can differ from what you could sell for." },
      { level: "estimate", text: "Regime, ADX, ATR, RSI, VWAP and NIFTY bias are indicator readings. Neither rule uses them. They touch confidence only through regime fit, which is a constant for the micro-trend rule." },
      { level: "gap", text: "On delayed Yahoo bars (about 15 minutes late) the micro-trend rule raises no alert and stock prices are old. The badge and the 'delayed' tag say so. Index VWAP is unverified: index bars carry no volume." },
      { level: "gap", text: "Stock bars end at 15:14, so a stock's daily close here is not the exchange's official close. GIFT NIFTY is a manual slot with no feed." },
      { level: "gap", text: "No study has shown an edge. Micro-trend is about zero on modelled prices and about minus 1.2% a trade on real premiums. Pullback made money because the stocks rose and, as a ten-trade book, trailed NIFTY held." },
    ],
    dont: [
      "Do not read a BUY badge or a green strip as advice. It states that the rule's test passed on the last closed bar.",
      "Do not read Unrealised as what the contract would fill at, and do not act on an alert past its valid time.",
      "Do not trust a card whose Data health is LAGGING or STALE. Check the price on Dhan.",
      "Do not read a few quotes in Fill costs as a rule. The median needs many samples, and FINNIFTY and SENSEX have wide, uneven spreads.",
      "Do not look for an alert on MIDCPNIFTY. It is for training only.",
    ],
  },
  "volume": {
    what: [
      "This tab shows how much is being traded right now compared with a normal day, read from the large stocks. Index bars carry no volume in the exchange feed, so it reads the desk's 18 large stocks (the tab says how many had bars) and their rupee turnover.",
      "Minute bars come from the Dhan feed the desk already holds. Past days come from the stored history. Buyers' share and pressure are estimates from bar shapes, not a count of buyers.",
      "It is a watchdog page. It tells you where unusual volume is. It does not tell you what to trade.",
    ],
    read: [
      { term: "Volume now (1 minute)", meaning: "The last closed minute's turnover (shares times price, in rupees) against the median of the same minute in the previous 20 full sessions. 1.0 times is usual, 2.0 times is double." },
      { term: "Last 5 minutes", meaning: "The last five minutes' turnover against the usual for those five minutes. This figure decides the state word Quiet, Normal or Busy." },
      { term: "Day so far", meaning: "Turnover since 09:15 against the usual for this time of day. A busy open lifts it all day, so it moves slowly later on." },
      { term: "Quiet", meaning: "Last 5 minutes below the quiet setting in config/desk.yaml (0.7 now) times the usual. An ordinary day reads about 1.0." },
      { term: "Busy", meaning: "Last 5 minutes at 1.5 times the usual or more, but no spike. Normal is anything between Quiet and Busy." },
      { term: "Spike", meaning: "The Spike score is at or above the alert level: 2.5 for the whole market, 4.0 for a single stock. It beats the other words. It says unusual size, not up or down." },
      { term: "Spike score", meaning: "How far the last 5 minutes' turnover is above its own mean in the last 60 full sessions at this time, in standard deviations of the log turnover. Needs at least 20 earlier sessions." },
      { term: "Buyers, last 5 minutes", meaning: "Share of the turnover counted as buying, 0 to 100%. Each minute is split by the setting rule, bvc: the minute's return against its recent spread. Mostly buying is 60% or more, mostly selling is 40% or less." },
      { term: "Stocks up and down", meaning: "How many basket stocks are above or below yesterday's close, and how many are unchanged." },
      { term: "Buying pressure", meaning: "An index card word. Pressure is the sum over stocks of (influence weight x net flow over normal turnover). Above 0.05 reads Buying, below minus 0.05 Selling, else Balanced. The line below says No clear pressure under 0.1." },
      { term: "Biggest influence", meaning: "The stocks that moved the index most in its last 60 sessions of minute returns (a fitted weight). The card also says what share of the index's minute moves the basket explains." },
      { term: "Training only", meaning: "A tag on an index card (MIDCPNIFTY). It is shown for study and is left out when an alert names the indices likely to move." },
      { term: "Buyers' share of resting orders", meaning: "Order book: shares waiting to buy against shares waiting to sell in the best five price steps, counted in shares and not in rupees. Orders can be pulled at any moment. The study did not test it." },
      { term: "Gap between prices", meaning: "The spread between best bid and best ask, in basis points (1 bp is 0.01%) of the mid price. Wider means thinner trading." },
      { term: "Likely to move", meaning: "In a market alert: up to two indices whose pressure in the spike is largest, each at 0.05 or more. It is the arithmetic of the weights, not a forecast of direction." },
    ],
    use: [
      "Before 09:15 the tab shows the last stored session and says the market is closed. Open the day's shape card to see which half hours are normally busy, so a busy first half hour is not a surprise.",
      "During the session, read the state word and the Last 5 minutes figure first. Then use the stock table (click a heading to sort) to see which stocks carry the volume, and the index cards to see where the weight sits.",
      "If a watch alert arrives, read it as a size warning: a larger move than usual has been likely in the study, in either direction. Check the price and your own plan before doing anything.",
      "After the close, pick the session date to replay the day and compare the alerts with what the index did. Read the What history says card once, so the limits stay in mind.",
    ],
    trust: [
      { level: "real", text: "Minute bars (volume, price) are what Dhan sent and what the store holds. Turnover is simply volume times close, so it is a measured number." },
      { level: "estimate", text: "Normal volume is the median of the previous 20 full sessions at the same minute. A full session has 300 or more minute bars. It is a statistic, and it is fixed in advance, not fitted to results." },
      { level: "estimate", text: "Buyers' share, pressure and influence weights are model estimates from bar shapes and fitted weights. They are not counts of buyers. Index volume is zero in the feed, so the basket stands in for the market." },
      { level: "real", text: "The study (24 Nov 2026, data to 7 Nov 2026) found a size effect: after a market spike the index's next 5 to 60 minutes were about 1.5 times as large as usual in all three periods." },
      { level: "real", text: "The same study found no direction: the flow's side was right 41% to 53% of the time, within 3 bp of nothing. An option buyer needs 5 to 10 bp. It calls the watch a size warning, never a signal." },
      { level: "gap", text: "The order book has been recorded only since 24 Nov 2026 and has never been studied. It also needs the Data API on. It is switched off after 3 failed reads in a row." },
      { level: "gap", text: "Whether the extra size pays an option buyer is untested. Option prices may already include it, because implied volatility tends to rise with volume. Single-stock spikes were counted but their aftermath was not studied." },
      { level: "gap", text: "The live view needs one-minute Dhan bars. On the Yahoo feed (2-minute, about 15 minutes late) the tab shows the last stored session instead and says so. Stock bars end at 15:14 since 3 Aug 2026, so 15:15 to 15:29 is empty." },
    ],
    dont: [
      "Do not read a spike as a direction. The study's own verdict is size, not direction, and a coin flip on side.",
      "Do not read Buyers' share, pressure or the order book as proof that someone is buying. They are estimates, and pressure was followed by a small give-back (1 to 2 bp) in the study.",
      "Do not treat the alerts as trade alerts. The page says so itself, and no paper trade is opened from them.",
      "Do not draw conclusions from one busy day. About 1.3 market alerts a day is normal on this setting.",
    ],
  },
  "sim": {
    what: [
      "The Simulator replays a stored market day one minute at a time. The strategy raises alerts, you take or skip them, and the desk manages the trade with the stop, target and trail rule.",
      "Candles come from stored one-minute bars of the instrument. Option prices are real traded prices where the local expired-options file has that strike and minute, and a model's prices everywhere else.",
      "Every trade is practice with dummy money (start balance Rs 100,000). It is kept apart from the paper book and never sends an order.",
    ],
    read: [
      { term: "Past / Live", meaning: "Past replays a stored day you choose. Live follows today on the running desk's feed at the market's own pace, and only for instruments the desk tracks. Live is disabled when the market is closed." },
      { term: "Alert", meaning: "A strategy signal on a candle that has closed. It lasts 10 market minutes. A new signal while an alert is waiting or a trade is open is dropped." },
      { term: "Take / Skip", meaning: "Take fills the trade at the open of the next minute, with slippage and charges. Skip drops the alert. Keys T and S." },
      { term: "Stop", meaning: "First stop is 1.5 times ATR(14) of 60-minute bars from the fill, shown with the rupees you would lose there. For an option the stop is judged on the index, the rupees are on the premium." },
      { term: "Target", meaning: "First target is 3 risks away from the fill, shown in rupees. From 80% of the way it moves out by half the planned move each time price comes within 20% of it." },
      { term: "Risk / Reward", meaning: "Rupees lost at the first stop and made at the first target, for one lot (index options) or one share (stocks), before the charges shown beside them." },
      { term: "reward to risk", meaning: "Reward divided by risk after charges. A number near 3 is the plan's design, not a forecast of what will happen." },
      { term: "real / modelled", meaning: "real: the premium is a traded price from the stored option bars (or today's through the desk in Live). modelled: the Black-76 model priced it. Stocks always show share prices." },
      { term: "First stop", meaning: "The meter runs from the first stop to the target. Marks at 25%, 50% and 80% are where the stop is raised: 25% breakeven plus costs and 5% of the move, 50% locks 35%, 80% trails." },
      { term: "Waiting for the fill at the next minute's open.", meaning: "You have taken the trade but it has no price yet. It fills at the next minute's open. If the day ends first it is cancelled and never counts as a result." },
      { term: "The strategy says close this trade.", meaning: "The strategy's own exit signal fired. With auto exit on, the trade closes at the next minute's open." },
      { term: "Why it closed", meaning: "Stop, Breakeven, Lock, Trail, Target, Exit signal, Day end, Closed (you pressed Close) or Data end. A GAP prefix means price jumped past the level and the fill is at the open." },
      { term: "Trades / Winners / Net / Equity", meaning: "This session's count, trades that closed above zero, net rupees after charges, and Rs 100,000 plus Net. Equity is practice money, not your account." },
      { term: "Smooth candles", meaning: "A drawing aid. Each minute is drawn along a path through its own open, high, low and close. Prices inside the minute are not real ticks." },
    ],
    use: [
      "Before 09:15, pick the instrument, strategy and a day, or press the dice for a random day and tick Hide the date to practise without knowing the day. Press Play and let it run to the first alert.",
      "When an alert appears, read the plan: stop, target, risk, reward and the real or modelled tag, then open Why this alert. Decide to Take or Skip the way you would on a live day, before you see what follows.",
      "While a trade is open, watch the meter and the stage line. Let the rule move the stop. Close by hand only if you would really do so on a live day, and note why.",
      "After the close, open the Trades tab and the Numbers tab. Compare winners, average win and average loss, and the share of modelled prices. Judge many days, not one.",
    ],
    trust: [
      { level: "real", text: "The candles are stored one-minute bars of the exchange feed. The server reveals only minutes up to the cursor, and a test proves signals equal those computed on bars cut at the cursor." },
      { level: "real", text: "For NIFTY and BANKNIFTY monthly options, the premium tagged real is a stored traded price of that strike in that minute, used only when that exact minute traded. Stored range is Nov 2021 to Nov 2026." },
      { level: "estimate", text: "Any premium tagged modelled is Black-76 with volatility from India VIX times 1.15, or from the last real fill. Other indices, other dates and minutes with no trade fall back to it. It ran about 1 point of premium a trade better than real." },
      { level: "estimate", text: "When your stop or target is touched inside a minute, the option price at that instant is interpolated between that minute's open and extreme. It is an estimate even when the bar is real." },
      { level: "estimate", text: "Fills are assumed at the next minute's open: shares slip 0.03% a side, options pay a 0.6% half spread plus 2 ticks. Recorded quotes show about 0.25% to 0.3% on NIFTY and BANKNIFTY and about 1.5% on FINNIFTY and SENSEX." },
      { level: "gap", text: "Charge rates are not checked against a real contract note. The app prints an UNVERIFIED warning at start. Treat rupee results as approximate until they are reconciled." },
      { level: "gap", text: "Live mode follows the desk's bars. If the desk is on Yahoo, bars run about 15 minutes behind the exchange, and real premiums for today need the Data API, otherwise they are modelled." },
      { level: "gap", text: "No study in this project has shown an index option rule that earns after costs (docs/SETUP_BOOK.md). A good practice day or week proves nothing about the strategy." },
    ],
    dont: [
      "Do not read a few winning days as proof; the replay can only show what a rule would have done, and the project's own sweeps found no tradable edge.",
      "Do not read a practice fill as a real fill. Real orders can fill worse in a fast market, queue, or miss the minute entirely.",
      "Do not trust a modelled premium for exact rupees, and do not use the Hide the date mode and then peek at the chart's day to pick trades with hindsight.",
    ],
  },
  "lab": {
    what: [
      "The Backtest Lab runs one strategy on one instrument and timeframe over stored history and shows what it would have made after charges, one unit per trade: one lot (futures, options) or one share.",
      "Signals are decided on closed candles and filled at the next candle's open with slippage and the Indian charge stack. Results are saved so Matrix, Leaderboard and Runs can compare many of them.",
      "Index and stock option trades are priced by a model (Black-76 with India VIX) in this tab, not by traded premiums.",
    ],
    read: [
      { term: "Net P&L", meaning: "Total rupees after brokerage, STT, exchange fee, SEBI fee, stamp duty and GST, for one unit a trade. Charge rates are not yet checked against a contract note." },
      { term: "Win rate", meaning: "Share of trades that ended above zero, shown with payoff (average win divided by average loss). A high win rate with a small payoff can still lose money." },
      { term: "Profit factor", meaning: "Gross wins divided by gross losses. Below 1.0 loses. The Lab's own flag for a useful cell is 1.2 or more, and the help says it starts to mean something with 30 or more trades." },
      { term: "Expectancy", meaning: "Average rupees per trade, with the same average in units of first risk shown as R. Positive after charges is the first bar to clear." },
      { term: "Max drawdown", meaning: "Worst peak to trough fall of cumulative rupees, shown with its share of the reference capital Rs 100,000. That capital is only for percentages, it does not size trades." },
      { term: "Sharpe", meaning: "From daily equity, annualised. The tile says it is noise on fewer than 20 trading days." },
      { term: "Costs", meaning: "Total charges and their share of gross moves. If the share is above about 30% the strategy pays the exchange more than it earns." },
      { term: "30 % rule", meaning: "Payoff you achieved against the 2.33 needed to break even at a 30% win rate, and the profit factor your exits would give at 30% wins. The tag is good at 1.1 or more." },
      { term: "premiums are Black-76 synthetic", meaning: "The option price is a formula from the index and India VIX (times 1.15), at about 0.65 delta, not a traded price." },
      { term: "Walk-forward", meaning: "Tune on an in-sample window, then judge on the next unseen window, slide forward, repeat. Only the joined out-of-sample trades score. It refuses to run below 12 sessions and aims for at least 3 folds." },
      { term: "PASS-LIKE OOS / WATCH / FAIL", meaning: "PASS-LIKE needs 30 or more out-of-sample trades, profit factor 1.2 or more and expectancy 0.05 R or more. WATCH needs 15 or more trades, positive expectancy and profit factor above 1. Anything else is FAIL." },
      { term: "Untuned OOS exp R", meaning: "The same windows run with default settings. If tuning does not beat it, the tuning is fitting noise." },
      { term: "score", meaning: "Leaderboard rank = expectancy R times the square root of trades (capped at 100 trades), minus 0.5 if profit factor is below 1. Blank below the minimum trades (10 on the Leaderboard, 5 on the Matrix)." },
      { term: "Positive", meaning: "A saved run with score above 0 and profit factor 1.2 or more. It counts lucky cells too, because nothing here adjusts for how many runs were tried." },
    ],
    use: [
      "Before the session, check the Data tab coverage: Yahoo gives only about 60 days of 2m to 15m bars, Dhan gives years of one-minute bars. A short window gives few trades and unreliable numbers.",
      "Choose one instrument, strategy and timeframe and press Run backtest. Read Trades first (below 30, ignore the rest), then Net P&L next to Costs, then Profit factor, Max drawdown and the 30 % rule line.",
      "Press Walk-forward for the same choice and compare Untuned OOS exp R with the tuned one. Use Optimize only to see how sensitive a setting is, never as a result.",
      "After the close, use Sweep, Matrix and Leaderboard to see how many cells are positive and how many are only lucky. Prefer rows marked OOS over in-sample rows, and treat the top of a list as the most flattering of many tries.",
    ],
    trust: [
      { level: "real", text: "Candle prices are stored exchange or Yahoo bars, cleaned for junk sessions. Signals use closed candles only and fills are at the next candle's open, so the replay logic has no look-ahead." },
      { level: "real", text: "The charge stack is applied to every trade, and slippage of 0.03% a side applies to stocks, futures and currency. Cost stress multiplies the charges so you can test a worse case." },
      { level: "estimate", text: "Option premiums are Black-76 with volatility from India VIX times 1.15 and a 0.6% half spread plus 2 ticks. The model has no skew or intraday volatility change and has run about 1 point of premium better than real." },
      { level: "estimate", text: "The charge rates are the author's own reading of the rules and are marked UNVERIFIED until reconciled with a real Dhan contract note. Net rupees can be off by some amount that nobody has measured." },
      { level: "estimate", text: "Futures ignore the basis (the futures price is taken equal to the spot). Stock backtests are intraday round trips on one share, which is not how the owner trades stocks as delivery of about Rs 10,000." },
      { level: "gap", text: "Many backtests on the same data produce winners by chance. The project's grid of 3,948 index option cells found 4 and confirmed none (docs/SETUP_BOOK.md). A top Leaderboard row is not an edge." },
      { level: "gap", text: "Out-of-sample trades under 30 are provisional, in-sample optimiser numbers are optimistic by design, and a sentiment gate only works once scores are imported." },
      { level: "gap", text: "The Matrix sorts by win rate, which flatters rules with many small wins and a few large losses. Check payoff and drawdown before reading it." },
    ],
    dont: [
      "Do not treat a high win rate, or a single good tuned run, as proof. Look at trades, out-of-sample results, drawdown and costs together.",
      "Do not read a backtest as a promise or as a recommendation. It shows only what a rule did on past data under assumed fills and prices.",
      "Do not compare an option result here with a real fill: premiums are modelled, spreads are one flat value, and fills assume the next candle's open.",
      "Do not pick the best of a sweep and trust it; ask how many runs there were and whether it also holds on walk-forward and on another instrument.",
    ],
  },
  "data": {
    what: [
      "This tab manages the price history the Lab, the simulator and the desk's warm-up read. You can start a download, see what is stored, run a quality check and explore a series. Prices come from Dhan (minute bars for about 5 years, daily for decades) and Yahoo (a free fallback with short intraday history).",
      "Coverage shows what is stored and what the Lab can build from it. The quality check and the explorer read the stored bars as they are.",
      "The Lab, the simulator and the live desk first clean mock-session days and two demergers. This tab does not.",
    ],
    read: [
      { term: "Coverage", meaning: "A table of what is stored. The default view, what the Lab can use, shows for each series and timeframe the widest history the Lab can build, using the finest bars that can make it." },
      { term: "what the Lab can use", meaning: "For each timeframe, the first and last day built from the widest stored base. Five years of Dhan 1m bars makes 5m, 15m and 60m five years long, not the 60 days of stored Yahoo bars." },
      { term: "bars stored", meaning: "The raw rows per series, resolution and source, with first date, last time and bar count. These are India times." },
      { term: "Days", meaning: "Calendar days from first to last (not trading sessions). It turns amber under 120 for any timeframe except 1d and 60m. Under 120 days is too short to judge a setup." },
      { term: "Built from", meaning: "Which stored resolution and source give the start of the history. Sources lists every source that feeds that timeframe, such as dhan + yahoo." },
      { term: "Backfill the corpus", meaning: "Starts a download job. With provider auto, Dhan is used when a Dhan Data API connection is at hand, else Yahoo. Only what is missing is fetched, from the last stored day. re-download everything refetches it all." },
      { term: "Dhan: 5-year 1-minute history", meaning: "Starts two jobs: daily bars for every series, and 1-minute bars for the desk watchlist, India VIX and the main indices. It needs a working Dhan token and takes 15 to 40 minutes." },
      { term: "Quality report (5m)", meaning: "Per series: first, last, Days, Bars, Short days, Zero-vol bars, Max gap %, Corp. action?. Short days: days with under 90% of the expected bars for the session length." },
      { term: "Zero-vol bars", meaning: "Bars with no volume. Shows n/a for indices and currencies because the feed gives no volume for them." },
      { term: "Max gap %", meaning: "The largest overnight gap, from one day's last close to the next day's first open, in percent." },
      { term: "Corp. action?", meaning: "A warning sign shown for a stock if any overnight gap exceeds 18%. It may be a split or a demerger the data was not adjusted for. Indices are never flagged." },
      { term: "Explore a series", meaning: "Charts and stats for one series: daily close with India VIX, return histogram, average bar range by time of day, sessions per month, weekday returns and a correlation table of the last 250 sessions." },
      { term: "Daily σ", meaning: "The standard deviation of daily returns over the last 250 days, in percent. Skew and kurtosis show how lopsided the returns are and how heavy their tails are." },
    ],
    use: [
      "Before the session, glance at Coverage in the what the Lab can use view and check the last date is yesterday for the series you rely on. A start date that is old or a Days figure in amber means little history.",
      "Run Start backfill (or the Dhan 5-year button once the token is renewed in Settings) outside market hours, because it takes a while. Watch the job list for FAILED and read its message.",
      "After a backfill, press Quality report and look for large Short days, a high Max gap % or the Corp. action sign. Those series need a second look before any Lab result is believed.",
      "Use Explore a series to see the typical day shape and return spread of an instrument. Read it as a description of the past, with the caveats below.",
    ],
    trust: [
      { level: "real", text: "The stored bars are what Dhan or Yahoo sent. Coverage dates and bar counts are counted straight from the store, so they are exact." },
      { level: "estimate", text: "Derived timeframes (5m, 15m and so on) are built from the 1-minute bars by the app. They are as good as the 1-minute data, and no more." },
      { level: "gap", text: "Yahoo is free and short: 1m for 7 days, 2m to 30m for 60 days, 60m for 2 years. Its daily history to 26 Oct 2026 uses Yahoo's own adjustments and differs from Dhan's minute bars (hygiene.py notes ITC 3.7%, Reliance 2%, Tata Motors 39.5%)." },
      { level: "gap", text: "The store holds Dhan's mock-trading Saturdays and two unadjusted demergers (ITC Hotels, Tata Motors, now TMPV). The Lab removes them first, but Quality and Explore show them as stored." },
      { level: "gap", text: "The quality check tests counts and overnight gaps only. It does not test duplicate, out-of-order or wrong-price bars, bar timing, or whether volume matches the exchange." },
      { level: "gap", text: "Stock minute bars end at 15:14 since 3 Aug 2026, so a day's last close here is not the official close. Index bars are separate." },
      { level: "estimate", text: "Real traded prices of NIFTY and BANKNIFTY monthly options (Nov 2021 to Nov 2026) are stored and the Simulator uses them. The Lab's own option runs still use a Black-76 model with India VIX (docs/11_BACKTEST_LAB.md), about 1 point of premium better than real prices per the setup book." },
    ],
    dont: [
      "Do not read a long Days figure as proof of a good test. Days counts calendar days, and a long history can still sit on cleaned-away or Yahoo-adjusted data.",
      "Do not trust Explore as an exact copy of what the backtest sees. Its subtitle says it shows the stored bars before the Lab's cleaning.",
      "Do not run a backfill during market hours or with an expired Dhan token. A job that falls back to Yahoo gives only a short history.",
      "Do not ignore the Corp. action sign. A stock with a flagged gap can produce fake big moves in any test.",
    ],
  },
  "news": {
    what: [
      "This tab holds a market pulse score, a score history and a news list. The pulse is the only part the desk makes by itself. It is a rule-based score from four inputs: stock breadth, index trend, India VIX and the NIFTY put/call ratio.",
      "The news list and any other sentiment score are empty until you or your own scorer post them. The desk fetches no news and runs no language model.",
      "The recorded scores exist so the Lab can gate backtests on sentiment. No study in this project has shown that the pulse predicts anything.",
    ],
    read: [
      { term: "Market pulse", meaning: "A number from 0 (bearish) through 0.5 (chop) to 1 (bullish). It is the weighted average of the available parts: breadth 0.35, index trend 0.30, India VIX 0.15, NIFTY PCR 0.20. Weights were chosen by hand." },
      { term: "bullish / chop / bearish", meaning: "The label on the pulse: bullish at 0.6 or more, bearish at 0.4 or less, chop between. Green at 0.6 or more, red at 0.4 or less, amber between." },
      { term: "Breadth", meaning: "Half the share of watchlist stocks up on the day, plus half the share above their day VWAP (the day's volume-weighted average price). The note shows the counts." },
      { term: "Index trend", meaning: "0.5 plus 0.5 times (indices trending up minus down) over the number of indices. Trending up means EMA20 above EMA50 on the desk's signal bars." },
      { term: "India VIX", meaning: "Scored 1 minus the VIX's percentile in the last 250 daily closes, so a low VIX scores bullish. That is a rule of thumb, not a fact. With no history it uses 1 minus (VIX minus 10) over 20." },
      { term: "NIFTY PCR", meaning: "Put open interest over call open interest in the nearest NIFTY expiry chain, scored (PCR minus 0.7) over 0.8, limited to 0 to 1. A high ratio scores bullish (contrarian). Needs the Data API; refreshed every 5 minutes." },
      { term: "Score history", meaning: "Chart of recorded MARKET scores by source on a minus 1 to plus 1 scale. The pulse is stored as (score minus 0.5) times 2, every 15 minutes while the market is open, with confidence fixed at 0.5." },
      { term: "Latest per scope", meaning: "The newest score for each scope (MARKET, INDEX, STOCK, SECTOR) and symbol, with horizon, confidence and time. Green above 0.1, red below minus 0.1." },
      { term: "Recent scores", meaning: "Every stored score row: time, scope, symbol, score, confidence (0 to 1, set by whoever posted it), horizon, source and a note." },
      { term: "News feed", meaning: "Headlines you or your scorer added. The coloured tag is the optional sentiment you gave: green above 0.1, red below minus 0.1, grey otherwise, and NEWS when none was given." },
      { term: "Post a score manually", meaning: "Adds a score you typed. The desk does not check it. Score runs minus 1 to plus 1, confidence 0 to 1." },
      { term: "Import historical scores for backtests", meaning: "Loads one row per day. A 0 to 1 scale is mapped to minus 1 to plus 1. Daily rows get a 09:15 India time stamp. In the Lab, pick a sentiment gate to use them." },
    ],
    use: [
      "Before 09:15 the pulse shows the last computed values, which may be from yesterday. Press recompute after the desk has started and bars are flowing.",
      "During the session read the four parts, not only the total. Breadth and trend come from the desk's own bars, so they are as fresh as the feed. VIX and PCR move slowly.",
      "Use the pulse as a one-line summary of what the desk already sees on its other tabs. It adds no new information.",
      "After the close, the score history is mainly useful if you want to test a sentiment gate in the Lab, and only once enough days are stored. Times on this tab are shown in India time.",
    ],
    trust: [
      { level: "real", text: "Breadth, trend, VIX and PCR are measured inputs: the desk's bars, the India VIX series and Dhan's option chain open interest. Each part's note shows the actual counts or values." },
      { level: "estimate", text: "The pulse score is a hand-made rule: the weights, the VIX percentile rule and the PCR scaling are rules of thumb. Nothing was fitted or tested against results." },
      { level: "gap", text: "There is no news fetcher and no sentiment model. The news list and extra scores are only what is posted by hand or by your own scorer. The tab never reads headlines itself." },
      { level: "gap", text: "No study or backtest in docs/ tests whether the pulse or any sentiment predicts moves. The Lab's sentiment gate exists, but its result is not in the setup book." },
      { level: "gap", text: "On the Yahoo feed (2-minute bars, about 15 minutes late) breadth and trend are late too. The PCR part is empty without the Data API, and the other parts are then re-weighted." },
      { level: "gap", text: "Times in Recent scores, Latest per scope and the news list are shown in India time, like the pulse's own time stamp. A time the server sends without a zone is shown as it is." },
      { level: "estimate", text: "Imported or posted scores are as good as their source. The desk stores them as given. A backtest gate uses the latest score at or before each bar, not older than 5 days." },
    ],
    dont: [
      "Do not read the pulse as a forecast. It describes the desk's current picture, and it has not been shown to lead price.",
      "Do not treat the news list as complete or current. If it is empty or old, that only means nothing was posted.",
      "Do not read a time on this tab as UTC: it is already India time.",
      "Do not backtest a sentiment gate on a few weeks of pulse history and call it proof.",
    ],
  },
  "mllab": {
    what: [
      "The Machine Learning Lab has two parts. The top card, Market states, labels each past session by direction and size and counts what usually came next. The table below gives statistical ranges for the next candle's close from five small models.",
      "Nothing here is a trained neural network or a hidden Markov model. The states are plain counting and the five models are ARIMA, SARIMA, GARCH, EWMA and Empirical. Neither part raises an alert or a trade.",
      "Numbers come from the desk's own bars (live feed joined with stored history). They describe how big a move is likely to be much better than which way it will go.",
    ],
    read: [
      { term: "Market states", meaning: "Each session gets a direction and a size. Bull: the day's close to close return is above +0.5 usual daily moves, bear below -0.5, flat in between. Usual is the spread of the 20 sessions before it." },
      { term: "calm / active", meaning: "Active: the day's high to low range is above the median range of the 20 sessions before it. Otherwise calm. On the strip a filled chip is active, a hollow chip is calm." },
      { term: "arrow chips", meaning: "Up arrow bull (green), right arrow flat (cyan), down arrow bear (red). Newest session is on the right and the last 30 are shown. Hover shows date, return and range." },
      { term: "Next session: leans BULL 35% (usual 34%)", meaning: "The share of past sessions in this instrument that were bull after the same pattern, pulled toward the usual rate when cases are few. It is a count of history, not a chance of profit." },
      { term: "usual", meaning: "How often that kind of session happens on any day in this instrument's own past. The black tick on each bar marks it. A lean near usual means almost no information." },
      { term: "conviction", meaning: "none, weak, moderate or strong. The edge over usual divided by its sampling error: below 1 none, 1 to 2 weak, 2 to 3 moderate, 3 or more strong (moderate if the edge is under 5 points)." },
      { term: "score", meaning: "The same measure as a number: 25 times that edge size, capped at 100. A score of 25 is 1 error above usual. It measures how unusual the count is, not how likely a profit is." },
      { term: "no lean", meaning: "Conviction none. Tomorrow looks like any other session as far as this history can tell." },
      { term: "n = ...", meaning: "Past cases behind the percentage (from the last session, or the last two when there are 60 or more such cases). Small n means a shaky figure." },
      { term: "Next session's range", meaning: "The same count for size: leans ACTIVE or CALM with its percentage and the usual one. The only part with a tested memory." },
      { term: "What followed this pattern / Runs", meaning: "Counts of what came after the last one or two sessions. Runs shows how often a run of 1, 2 or 3 or more bull or bear sessions went on one more, against usual, with cases." },
      { term: "Checked on N past sessions", meaning: "How often the model named the next direction right, fitted only on sessions before each one, against always naming the most common direction. Size has its own pair." },
      { term: "±x.xx% 68%", meaning: "In a model cell: half width of the band that should hold the next close about 68% of the time, with its low to high price, and the 95% band beneath. This is the range, not a direction." },
      { term: "last 100: 68% band held / 95% band held", meaning: "How often the model's own last 100 bands held the real next close. The page says about 7 in 10 and 93 in 100 were seen. With only 100 forecasts the figure moves a few points." },
      { term: "up NN · not a signal", meaning: "The model's probability that the next close is higher. Tested: right no more often than a coin. EWMA is always exactly 50%." },
      { term: "Move size / opening candle", meaning: "Move size is calm (lowest third), normal or active (highest third) of this instrument's own past volatility: the current width ranked among its earlier values. It needs 80 returns. The opening candle has its own wider band because it carries the overnight gap." },
    ],
    use: [
      "Before 09:15, open the tab on the daily view (1d) and the Market states card. Read the Next session's range line first: if it leans ACTIVE, a wider day has been more likely in the past data. Treat the direction lean as background and never as a call.",
      "At the open, switch the table to 15m or 60m. Look at Move size and the opening candle note: the opening candle band is wider on purpose. Use the 68% band half width as a yardstick for how far one candle usually travels.",
      "During the day, compare your stop or target distance with the band. A stop nearer than one band half width is inside ordinary noise. A close outside the 95% band is unusual, but the page's tests found nothing about what comes after it.",
      "After the close, check Checked on N past sessions and the cases n. If n is small or conviction is none or weak, the card says almost nothing. Never use this tab to decide direction.",
    ],
    trust: [
      { level: "real", text: "Sessions, their returns and ranges, the chips and the counts are measured from stored and live bars. Usual and the percentages are real counts of that instrument's history." },
      { level: "estimate", text: "Percentages are counts pulled toward usual by 10 pseudo-sessions. With about 290 sessions on the page they rest on tens of cases, so a single percentage can easily move 5 to 10 points by luck." },
      { level: "estimate", text: "Four models draw the band from a normal curve fitted on the last 1000 returns at most (Empirical uses percentiles of the last 250). Returns have fat tails, so the 68% band held about 69 to 76% for the fitted models and the 95% band about 90 to 95% in tests." },
      { level: "estimate", text: "The Next session's range line carries real memory: after an active session the next was active 57% of the time against 49% usual (23 instruments, about 55,000 sessions). Move size was followed by moves about 1.4 to 1.6 times larger when active than when calm. Both say how big, not which way." },
      { level: "gap", text: "Direction has almost no memory. Two bulls in a row was followed by a bull 33% of the time in indices and 29% in stocks, the same as usual, and the walk-forward hit rate was 42.1% against 42.5% for always naming the most common." },
      { level: "gap", text: "The up probability is a coin flip: on all five timeframes and 23 instruments, the next candle closed up 47% to 52% of the time whether all models agreed or not (MLLAB_CONFIDENCE, 13 Nov 2026)." },
      { level: "gap", text: "Following a lean paid about +3 basis points a session before costs (t 0.7). A stock round trip costs about 16 basis points and an index option needs 5 to 10 basis points of index move to cover costs. Nothing here clears it." },
      { level: "gap", text: "The page's own evidence is far thinner than the study's: the desk loads about 420 calendar days of daily bars, so each card has roughly 270 stated sessions and a walk-forward check of only about 20. The study had about 2,400 stated sessions per instrument." },
    ],
    dont: [
      "Do not read the percentage or conviction as the chance your trade will make money. It is only how often a similar pattern was followed by that kind of session in past data.",
      "Do not read score or conviction as a trading signal; a strong lean was no better than naming the most common direction, and a bear lean was followed by a rise as often as a bull lean.",
      "Do not trust a 'up 58%' figure. In tests it behaves like 50%. Do not trust a state card that says Only N stated sessions or shows n under about 60.",
      "Do not use the bands for the day's opening candle as if it were an ordinary candle, and do not read a band that held 7 times in 10 as a guarantee for the next one.",
    ],
  },
  "settings": {
    what: [
      "Settings holds the Dhan credentials and token tools, the Telegram phone alerts, the watchlist file's view and a read-only view of the configuration.",
      "Credentials are saved to a local .env file and never shown back. The page shows only 'set', 'not set' or 'wrong shape'. Buttons call Dhan or Telegram to test or renew.",
      "Status lines come from the engine and the broker's cached profile. Nothing here changes a trading rule.",
    ],
    read: [
      { term: "Client ID · Access token · Data API token", meaning: "Shown masked or as 'not set'. The Data API token is optional because the plan is on the account. The page never shows a token back. Press Save credentials to store new ones." },
      { term: "Token valid till", meaning: "When the broker token ends, in IST, with time left. 'expired' in red means the desk cannot read the market. It comes from the cached profile, so refresh the portfolio on Home if it looks old." },
      { term: "Self-renew (TOTP)", meaning: "'configured' when the Dhan PIN and TOTP secret are in .env. The desk then mints a fresh 24-hour token before the old one ends. A red line shows the last error. 'not configured' means you paste a token daily." },
      { term: "Account reads", meaning: "'attached' means the free account reads work: profile, funds, holdings, positions, ledger, trades. 'no adapter' means no usable credentials. These tiles are not used by any rule." },
      { term: "Data API plan", meaning: "'active' when the profile says dataPlan is Active and the token is not expired. Only then does the desk use Dhan one-minute bars, quotes and the option chain. Otherwise it falls back to delayed Yahoo bars." },
      { term: "Market feed", meaning: "'Dhan (live)' means the engine reads Dhan. 'synthetic mock' means made-up prices. Nothing on screen then reflects the real market. Press Go live once the token is valid." },
      { term: "Desk bars", meaning: "The desk's bar source and size, delayed or live, and the socket: 'connected' with how many instruments, or 'down'. Seeing 'dhan 1m live' with the socket connected is the healthy case." },
      { term: "trading API OK · data API OK", meaning: "Result of Test entitlements. The trading API must be OK. The data API shows 'not available' without the paid plan. The table lists each endpoint with its error. The scrip master download takes a while." },
      { term: "Renew token now · Go live", meaning: "Renew token now mints a token from the stored PIN and TOTP secret. Go live checks the Data API and switches from the synthetic market to the Dhan feed without a restart." },
      { term: "Bot token · Chat id", meaning: "Telegram status. Bot token: set, not set or wrong shape (digits, a colon, then letters and digits). Chat id: set or not set. Use Find my chat id after pressing Start on your bot." },
      { term: "Phone alerts on / off", meaning: "'on' means the Telegram channel is in use. 'off' shows the reason, for example the master switch is off in config.yaml or the token is missing. 'Send a test message' proves only that Telegram accepted it." },
      { term: "Token reminders", meaning: "Sent to the phone 60, 30, 15 and 5 minutes before the token ends, then 'expired'. While expired, one at 08:30 IST each trading day. A renewal sends 'Dhan token renewed'. Each is sent once per token." },
      { term: "Live Desk watchlist", meaning: "The instruments and strategy chips from config/desk.yaml, like microtrend@3m and pullback@1d. A pencil mark ✎ means that slot has its own settings. Reload desk config reads the file again. Poll now fetches bars at once." },
      { term: "Desk configuration · Mode PAPER", meaning: "Read-only view of config.yaml: mode, address and the Risk, Backtest and Data blocks. Mode PAPER or LIVE only changes what the old engine's journal records. The system never places an order in either." },
    ],
    use: [
      "Before 09:15, check Token valid till and Market feed. If the token has under a couple of hours left, press Renew token now or paste a fresh token and press Save credentials.",
      "If Market feed says 'synthetic mock', press Go live once the token is valid, then check Desk bars shows Dhan with the socket connected.",
      "Set up Telegram once: create a bot in BotFather, paste its token and Save, press Start in the bot, press Find my chat id, then Send a test message.",
      "After you edit config/desk.yaml, press Reload desk config. Use Poll now if bars look old.",
      "Run Test entitlements after a plan change, not every day.",
    ],
    trust: [
      { level: "real", text: "'set' and 'not set' reflect what is in the local .env file, and tokens are never shown back or logged." },
      { level: "real", text: "Token valid till and Data API plan are the broker's own figures from the profile. Test entitlements results are live answers from Dhan at that moment." },
      { level: "estimate", text: "Time left is the profile's expiry minus this computer's clock. A wrong clock or an old cached profile gives a wrong countdown." },
      { level: "gap", text: "'Send a test message' shows Telegram accepted the message, not that your phone displayed it. Nothing is sent while the computer sleeps, is offline or the desk is stopped." },
      { level: "gap", text: "Some office and institute networks block api.telegram.org. Then the test fails with 'Could not reach Telegram' and messages wait in a queue until a network allows it." },
      { level: "gap", text: "A missing 09:05 'Desk up' message means the desk is down. At most 8 alerts are sent one by one, best confidence first, and the rest go into one digest." },
      { level: "gap", text: "The Risk block shown here belongs to the old Bank Nifty engine. The Live Desk's alerts and paper trades have no risk governor. The page says so beside the block." },
    ],
    dont: [
      "Do not paste credentials anywhere but the fields here, and do not share the .env file or screenshots of it.",
      "Do not read 'Phone alerts on' as proof that alerts will reach you. Test it and keep the app on your phone.",
      "Do not treat Mode LIVE as meaning the desk trades. It places no orders in any mode.",
      "Do not leave the feed on 'synthetic mock' and trust any alert.",
    ],
  },
};
// CHEATS-DATA-END

const CHEAT_PARTS = [
  ["what", "What this tab is"],
  ["read", "How to read it"],
  ["use", "How to use it on a market day"],
  ["trust", "How far to trust it"],
  ["dont", "Do not"],
];
const CHEAT_TAGS = { real: "Real data", estimate: "Estimate", gap: "Gap" };

function cheatEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function cheatPart(id, title, inner) {
  return inner ? '<div class="cheat-part cheat-' + id + '"><h4>' + cheatEsc(title) + "</h4>" + inner + "</div>" : "";
}

function cheatList(items) {
  return items.length ? '<ul class="cheat-list">' + items.map((t) => "<li>" + cheatEsc(t) + "</li>").join("") + "</ul>" : "";
}

function cheatHtml(key) {
  const c = CHEATS[key];
  if (!c || typeof c !== "object") return "";
  const arr = (v) => (Array.isArray(v) ? v : []);
  const read = arr(c.read).filter((r) => r && typeof r === "object");
  const trust = arr(c.trust).filter((r) => r && typeof r === "object");
  const readHtml = read.length
    ? '<dl class="cheat-terms">' + read.map((r) => "<dt>" + cheatEsc(r.term) + "</dt><dd>" + cheatEsc(r.meaning) + "</dd>").join("") + "</dl>"
    : "";
  const trustHtml = trust.length
    ? '<ul class="cheat-list">' + trust.map((r) => {
        const lv = Object.prototype.hasOwnProperty.call(CHEAT_TAGS, r.level) ? r.level : "gap";
        return '<li><span class="cheat-tag cheat-tag-' + lv + '">' + CHEAT_TAGS[lv] + ":</span> " + cheatEsc(r.text) + "</li>";
      }).join("") + "</ul>"
    : "";
  return (
    cheatPart("what", CHEAT_PARTS[0][1], cheatList(arr(c.what))) +
    cheatPart("read", CHEAT_PARTS[1][1], readHtml) +
    cheatPart("use", CHEAT_PARTS[2][1], cheatList(arr(c.use))) +
    cheatPart("trust", CHEAT_PARTS[3][1], trustHtml) +
    cheatPart("dont", CHEAT_PARTS[4][1], cheatList(arr(c.dont)))
  );
}

function cheatMount(root) {
  const scope = root || (typeof document !== "undefined" ? document : null);
  if (!scope || !scope.querySelectorAll) return;
  scope.querySelectorAll("details.cheat[data-cheat]").forEach((el) => {
    const body = el.querySelector(".cheat-body");
    if (body) body.innerHTML = cheatHtml(el.getAttribute("data-cheat"));
  });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => cheatMount());
  else cheatMount();
}
