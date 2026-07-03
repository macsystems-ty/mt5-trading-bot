"""
test_double_top_bottom.py

Tests DOUBLE TOP / DOUBLE BOTTOM as a new entry signal -- a REVERSAL
pattern, deliberately opposite to our normal trend-following entries.

  - DOUBLE TOP: forms during an UPTREND (per a slower 2H/4H EMA),
    two peaks at roughly the same level separated by a meaningful
    pullback -> signals SELL.
  - DOUBLE BOTTOM: forms during a DOWNTREND, two troughs at roughly
    the same level -> signals BUY.

Entry triggers once one of our EXISTING 10 candlestick patterns
confirms on the breakdown/breakout candle. Exit mechanics are
COMPLETELY UNCHANGED.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_double_top_bottom.py
"""

import importlib.util
import os

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))
MARKETS_TO_CHECK = ["1HZ25V", "1HZ75V", "1HZ90V", "1HZ100V", "R_100"]

FIXED_STOP_PCT = {
    "1HZ25V": 0.057,
    "1HZ75V": 0.344,
    "1HZ90V": 0.417,
    "1HZ100V": 0.476,
    "R_100": 0.463,
}

EMA_TIMEFRAMES_TO_TEST = ["2h", "4h"]
PEAK_TOLERANCE_PCTS_TO_TEST = [0.05, 0.15, 0.3]
MIN_PULLBACK_PCT = 0.5
SWING_LOOKBACK = 3

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_swing_points(df, lookback):
    highs = df["high"].values
    lows = df["low"].values
    peaks = []
    troughs = []

    for i in range(lookback, len(df) - lookback):
        window_highs = highs[i - lookback: i + lookback + 1]
        window_lows = lows[i - lookback: i + lookback + 1]
        if highs[i] == max(window_highs):
            peaks.append((i, highs[i]))
        if lows[i] == min(window_lows):
            troughs.append((i, lows[i]))

    return peaks, troughs


def build_slow_ema_trend(bs, df, ema_timeframe):
    resampled = df.resample(ema_timeframe).agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).dropna()
    result = bs.indicators.add_all_indicators(resampled)
    ema_series = result["ema_14"]
    return ema_series, resampled


def simulate_double_top_bottom(bs, df, fixed_stop_pct, ema_timeframe, peak_tolerance_pct, min_pullback_pct, enable_pattern):
    if not enable_pattern:
        return []

    import backtester_sr_engulfing as sre

    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df.index

    ema_series, _ = build_slow_ema_trend(bs, df, ema_timeframe)
    ema_aligned = ema_series.reindex(times, method="ffill")

    peaks, troughs = find_swing_points(df, SWING_LOOKBACK)

    trades = []
    open_trade = None
    stop_loss_price = None
    favorable_candle_count = 0
    stage3_active = False

    peak_at_index = {idx: price for idx, price in peaks}
    trough_at_index = {idx: price for idx, price in troughs}

    used_peak_pairs = set()
    used_trough_pairs = set()

    loop_start = SWING_LOOKBACK * 2 + 2

    for i in range(loop_start, len(df)):
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]

        if open_trade is not None:
            direction = open_trade["direction"]
            entry_price = open_trade["entry_price"]
            should_close = False
            exit_level = None

            if stage3_active:
                window_start = max(0, i - bs.STAGE3_WINDOW)
                if direction == "BUY":
                    new_stop = min(lows[window_start:i])
                    if new_stop > stop_loss_price:
                        stop_loss_price = new_stop
                    should_close = current_low <= stop_loss_price
                else:
                    new_stop = max(highs[window_start:i])
                    if new_stop < stop_loss_price:
                        stop_loss_price = new_stop
                    should_close = current_high >= stop_loss_price
                if should_close:
                    exit_level = stop_loss_price
            else:
                if direction == "BUY":
                    hit_stop = current_low <= stop_loss_price
                else:
                    hit_stop = current_high >= stop_loss_price
                if hit_stop:
                    should_close = True
                    exit_level = stop_loss_price

                favorable = (
                    current_close > current_open if direction == "BUY"
                    else current_close < current_open
                )
                if favorable:
                    favorable_candle_count += 1
                    if favorable_candle_count >= bs.NUM_FAVORABLE_CANDLES_REQUIRED:
                        stop_loss_price = entry_price
                        stage3_active = True
                else:
                    favorable_candle_count = 0

            if should_close:
                exit_price = sre.exit_fill_price(exit_level, direction)
                if direction == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100

                trades.append({"pct_change": pct_change, "initial_stop_distance_pct": fixed_stop_pct})
                open_trade = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False

            continue

        ema_at_i = ema_aligned.iloc[i]
        if ema_at_i is None or ema_at_i != ema_at_i:
            continue

        if i in peak_at_index and current_close > ema_at_i:
            current_peak_price = peak_at_index[i]
            # CRITICAL FIX: only compare against the MOST RECENT prior
            # peak, not all of history -- a genuine double top is a
            # LOCAL structure. Matching any similarly-priced peak from
            # anywhere in the past (the original behavior) produced
            # thousands of coincidental, meaningless matches.
            prior_peaks = [(idx, p) for idx, p in peaks if idx < i]
            if prior_peaks:
                earlier_idx, earlier_price = prior_peaks[-1]
                tolerance = earlier_price * (peak_tolerance_pct / 100)
                if abs(current_peak_price - earlier_price) <= tolerance and (earlier_idx, i) not in used_peak_pairs:
                    between_low = min(lows[earlier_idx:i + 1])
                    pullback_pct = (earlier_price - between_low) / earlier_price * 100
                    if pullback_pct >= min_pullback_pct:
                        used_peak_pairs.add((earlier_idx, i))
                        for j in range(i, min(i + 5, len(df) - 1)):
                            recent_candles = [
                                bs.Candle(open=opens[k], high=highs[k], low=lows[k], close=closes[k])
                                for k in range(max(0, j - bs.MAX_CANDLES_NEEDED + 1), j + 1)
                            ]
                            if len(recent_candles) < bs.MAX_CANDLES_NEEDED:
                                continue
                            matched_pattern = bs.any_pattern_matches("SELL", recent_candles)
                            if matched_pattern is not None:
                                fill_price = sre.entry_fill_price(closes[j], "SELL")
                                initial_stop = fill_price * (1 + fixed_stop_pct / 100)
                                open_trade = {"direction": "SELL", "entry_price": fill_price}
                                stop_loss_price = initial_stop
                                favorable_candle_count = 0
                                stage3_active = False
                                break

        if open_trade is None and i in trough_at_index and current_close < ema_at_i:
            current_trough_price = trough_at_index[i]
            prior_troughs = [(idx, p) for idx, p in troughs if idx < i]
            if prior_troughs:
                earlier_idx, earlier_price = prior_troughs[-1]
                tolerance = earlier_price * (peak_tolerance_pct / 100)
                if abs(current_trough_price - earlier_price) <= tolerance and (earlier_idx, i) not in used_trough_pairs:
                    between_high = max(highs[earlier_idx:i + 1])
                    pullback_pct = (between_high - earlier_price) / earlier_price * 100
                    if pullback_pct >= min_pullback_pct:
                        used_trough_pairs.add((earlier_idx, i))
                        for j in range(i, min(i + 5, len(df) - 1)):
                            recent_candles = [
                                bs.Candle(open=opens[k], high=highs[k], low=lows[k], close=closes[k])
                                for k in range(max(0, j - bs.MAX_CANDLES_NEEDED + 1), j + 1)
                            ]
                            if len(recent_candles) < bs.MAX_CANDLES_NEEDED:
                                continue
                            matched_pattern = bs.any_pattern_matches("BUY", recent_candles)
                            if matched_pattern is not None:
                                fill_price = sre.entry_fill_price(closes[j], "BUY")
                                initial_stop = fill_price * (1 - fixed_stop_pct / 100)
                                open_trade = {"direction": "BUY", "entry_price": fill_price}
                                stop_loss_price = initial_stop
                                favorable_candle_count = 0
                                stage3_active = False
                                break

    return trades


def compound(trades, starting_balance, risk_pct):
    balance = starting_balance
    for t in trades:
        if not t["initial_stop_distance_pct"]:
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t["initial_stop_distance_pct"] / 100)
        balance += position_value * (t["pct_change"] / 100)
        if balance <= 0:
            balance = 0
    return balance


def main() -> None:
    for symbol in MARKETS_TO_CHECK:
        print("=" * 100)
        print(f"MARKET: {symbol}")
        print("=" * 100)

        try:
            bs = load_market_module(symbol)
            candles = bs.load_candles()
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIPPED: {exc!r}\n")
            continue

        fixed_pct = FIXED_STOP_PCT[symbol]

        print(f"  {'EMA TF':>8} | {'Tolerance':>10} | {'Trades':>7} | {'Win Rate':>9} | {'Compounded Return':>18}")
        print("  " + "-" * 70)

        for ema_tf in EMA_TIMEFRAMES_TO_TEST:
            for tol in PEAK_TOLERANCE_PCTS_TO_TEST:
                try:
                    trades = simulate_double_top_bottom(bs, candles, fixed_pct, ema_tf, tol, MIN_PULLBACK_PCT, enable_pattern=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {ema_tf:>8} | {tol:>9}% | SKIPPED: {exc!r}")
                    continue

                if not trades:
                    print(f"  {ema_tf:>8} | {tol:>9}% | {0:>7} | {'N/A':>9} | {'N/A':>18}")
                    continue

                balance = compound(trades, STARTING_BALANCE, RISK_PCT)
                ret = (balance / STARTING_BALANCE - 1) * 100
                wins = sum(1 for t in trades if t["pct_change"] > 0)
                win_rate = wins / len(trades) * 100 if trades else 0
                print(f"  {ema_tf:>8} | {tol:>9}% | {len(trades):>7} | {win_rate:>8.1f}% | {ret:>+17.1f}%")

        print()


if __name__ == "__main__":
    main()