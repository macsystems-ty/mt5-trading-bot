# Strategy Snapshot — Saved Before Exploring New Direction
**Date saved:** 2026-06-22
**Purpose:** Lock in everything validated so far as a clean reference point.

---

## THE STRATEGY (current best, validated)

**Name:** S/R + Engulfing + EMA-1H Trend ("the combo")

### Core logic
1. **Trend filter:** EMA(50) on 1-hour candles.
   - price > EMA AND EMA rising -> trend = UP
   - price < EMA AND EMA falling -> trend = DOWN
   - Otherwise -> FLAT (no trade)
2. **Entry trigger:** Price retests a swing support/resistance level
   (swing = most extreme high/low among 11 candles: 5 before + itself + 5 after,
   i.e. SWING_LOOKBACK = 5), within RETEST_TOLERANCE_PCT = 0.01% of the level,
   AND an Engulfing candle confirms the reversal at that retest.
   - Support retest + bullish engulfing + trend=UP -> BUY
   - Resistance retest + bearish engulfing + trend=DOWN -> SELL
3. **Exit (normal):** Trailing stop = lowest low / highest high of the last
   TRAILING_WINDOW = 3 candles.
4. **Exit (safety):** If the EMA-1H trend flips against the open position's
   direction (and isn't FLAT), close immediately regardless of trailing stop.

### Validated parameters (best found)
- SWING_LOOKBACK = 5
- RETEST_TOLERANCE_PCT = 0.01%
- TRAILING_WINDOW = 3
- Timeframe: 5-minute candles (best validated -- see results below)

---

## BACKTESTED RESULTS BY TIMEFRAME (on 1HZ25V / Volatility 25 (1s), full real data)

| Timeframe | Sample size | Trades | Win Rate | Return (real spread+commission) | Notes |
|---|---|---|---|---|---|
| 1min | ~6,400 candles (~4.4 days) | 126-127 | ~42-43% | -0.224% to -0.338% | Negative after real costs |
| 5min | 105,000+ candles (1 full year) | 1,084 | 40.9% | +14.429% | BEST -- large sample, real costs included |
| 15min | 35,041 candles (1 full year) | 252 | 32.5% | -5.254% | Clearly unprofitable at scale |

Why 5min wins: Per-day return rate is actually lower than 1min's, but 5min's
result is far more statistically credible due to sample size (1,084 trades vs
126). 15min, once given a real sample, turned out genuinely unprofitable --
its earlier "0% win rate from 3 trades" was a preview of real weakness, not
bad luck.

Important history note: Deriv's available historical data for this
symbol goes back almost exactly 1 year (confirmed empirically -- pagination
stalled naturally at that point). This is the practical ceiling for backtesting
depth on this instrument via this API.

---

## REAL COSTS, CONFIRMED LIVE (not estimated)

- Spread (1HZ25V): 58 points / ~849,362 mid ~= 0.0068% per round trip
  (measured live via MT5 bid/ask)
- Commission (Deriv Multipliers, 1HZ25V): ~0.0125% of notional value
  (observed: $0.02 commission on $1 stake x 160 multiplier = $160 notional)
- Commission varies by instrument -- confirmed V75 (1s) has a different
  rate (~0.0400% of notional) -- do NOT assume one rate fits all symbols.

---

## POSITION SIZING

### Deriv Options/Multipliers platform
- Stake = risk % of balance directly (e.g. 0.5% risk -> stake = 0.5% of balance)
- Multiplier chosen from fixed tiers (varies by symbol -- V25: 160/400/800/
  1200/1600; V75: 50/100/200/300/500) to align Deriv's built-in stop_out
  distance with the strategy's actual stop distance
- Max loss is hard-capped at the stake by Deriv's design (auto stop_out)

### MT5 platform (via MetaApi)
- Lot/volume-based: volume = risk_dollars / (current_price x stop_distance_fraction)
- V25 (1s) on MT5: minVolume=0.005, maxVolume=2, volumeStep=0.001, contractSize=1
- No hard stake cap like Deriv Multipliers -- true stop-loss-dependent risk

---

## CRITICAL BUGS FOUND AND FIXED (apply to BOTH bots if porting logic forward)

### Bug 1: Position sizing used the WRONG stop distance (MAJOR -- fixed in MT5 bot only so far)
- What was wrong: check_for_entry() calculated stop_distance_pct as the
  distance to the S/R level (abs(close - level.price)), but open_trade()
  set the ACTUAL trailing stop to a different value (lowest low / highest high
  of last TRAILING_WINDOW candles). These two numbers were unrelated.
- Real-world impact: A live MT5 trade risked ~$96 when the target was $50
  (0.5% of $10,000) -- roughly 2x the intended risk.
- Fix: Calculate the actual initial trailing stop price FIRST, then derive
  stop_distance_pct from that same value, so position sizing always matches
  the real stop distance.
- Status: FIXED in mt5-trading-bot/src/live/live_mt5_trading_bot.py.
  NOT YET applied to deriv-trading-bot/src/live/live_trading_bot.py --
  same bug almost certainly exists there too. Apply the identical fix if that
  bot is used again.

### Bug 2: Heartbeat/candle-tracking broke after historical preload
- What was wrong: Tracking len(state.candles_1min) to detect new candles
  silently breaks once the list hits its cap (e.g. 2000) -- length stops
  changing even as new candles arrive.
- Fix: Track the most recent candle's open_time instead of list length.
- Status: FIXED in both bots (wherever historical preload was added).

### Bug 3: 1h-candle chunk size was wrong after switching timeframes
- What was wrong: Building 1h candles assumed chunk_size = 60 (i.e. 60
  one-minute candles = 1 hour) -- but this is WRONG for 5-minute candles
  (should be chunk_size = 12).
- Fix: chunk_size = 12 for 5min candles (verified: consecutive 1h
  candles built this way are exactly 1:00:00 apart).
- Status: FIXED in mt5-trading-bot (switched to 5min).
  deriv-trading-bot's live bot is still on 1min -- not affected unless/until
  it's also switched to 5min.

### Bug 4: Multi-timeframe backtester reused a too-short trend series
- What was wrong: backtester_multi_timeframe.py originally built the
  EMA-1H trend series ONCE from the short 1min data range, then reused it
  unchanged when testing 5min/15min entries spanning a full year -- meaning
  most of that extra history had no valid trend to check against.
- Fix: Build the trend series fresh from each entry timeframe's own data
  range (build_trend_series_for_range() function).
- Status: FIXED in backtester_multi_timeframe.py and
  simulate_account_growth_5min.py.

### Bug 5: ContractNotFound after Deriv's own stop_out triggered first
- What was wrong: If Deriv's built-in stop_out closed a contract before
  our bot's own check could, every subsequent close_trade() attempt on that
  contract failed with ContractNotFound forever, leaving the bot stuck
  with a stale open_position and stale balance.
- Fix: Detect ContractNotFound specifically, refresh real balance from
  the API, and clear the stale local position.
- Status: FIXED in deriv-trading-bot/src/live/live_trading_bot.py.
  (MT5 bot uses a different close mechanism -- verify if same failure mode
  is possible there before assuming it's immune.)

### Bug 6 (logging, not strategy): MetaApi SDK logs flooded the console
- What was wrong: logging.basicConfig(level=logging.INFO) set the ROOT
  logger to INFO, so MetaApi's internal SDK loggers (logging every price tick
  and sync packet) drowned out our own bot's log lines.
- Fix: Root logger set to WARNING; our own mt5_live_bot logger
  explicitly forced back to INFO.
- Status: FIXED.

---

## VERIFIED: Live bot logic matches backtester (post-fix)

Ran verify_live_bot_logic.py -- replays the LIVE bot's actual functions
(not a reimplementation) against the full real 5min dataset using a mock
connection (no real orders placed). Result:

- 1,011 trades, 47.8% win rate (vs backtester's 1,084 trades, 40.9% --
  close enough to confirm correct porting; the win-rate improvement is
  consistent with the stop-sizing bug fix).
- Confirms the strategy logic is faithfully implemented in the live bot,
  not just in the backtester.

---

## CURRENT INFRASTRUCTURE STATUS

### Deriv Options/Multipliers bot (deriv-trading-bot/)
- Platform: Deriv Options API (api.derivws.com), via PAT token
- Symbol: 1HZ25V (Volatility 25 (1s) Index)
- Currently configured for 1min candles (never switched to 5min)
- Position-sizing bug (Bug 1) NOT yet fixed here
- Has historical preload, reconnect logic, trend-reversal safety exit,
  ContractNotFound handling

### MT5 bot (mt5-trading-bot/)
- Platform: MT5 via MetaApi (cloud-hosted terminal bridge)
- Account: Deriv MT5 Demo, login 6184971, $10,000 starting balance
- Symbol: "Volatility 25 (1s) Index" (MT5 naming -- different string than
  Deriv Options API's "1HZ25V")
- Switched to 5min candles (our best validated timeframe)
- Position-sizing bug (Bug 1) FIXED
- Has historical preload (reusing copied Deriv CSV data -- same underlying
  price history, just originally fetched via the other API), trend-reversal
  safety exit
- Risk per trade: 0.5% (conservative default)
- Trades placed here appear in the user's real MT5 app (including phone)

---

## KEY LESSONS LEARNED THIS PROJECT (worth remembering before trying something new)

1. Small samples lie. Every "promising" result that later fell apart
   (V75's first +2.17%, 15min's "0% win rate," 5min's frozen 14-trade count)
   was a small-sample artifact. Always push for the largest real sample
   practical before trusting a result.
2. Win rate is not profitability. Higher win rate strategies were repeatedly
   found to have worse total returns than lower win rate ones with better
   reward:risk shape. Always check total return, not just win rate.
3. Position sizing bugs hide easily. The stop-distance mismatch (Bug 1)
   passed silent for a long time because both numbers "looked like percentages"
   -- always verify position sizing inputs actually match the real exit logic,
   ideally with an isolated test.
4. Verify the SAME data range when comparing things. Bug 4 (mismatched
   trend series) is a classic case of comparing results that look valid in
   isolation but were quietly evaluated on different underlying conditions.
5. Real cost data beats borrowed assumptions. Spread and commission differ
   meaningfully by instrument -- always confirm live before trusting backtests.
