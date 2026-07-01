"""
test_profit_threshold_trigger.py

Tests adding a PROFIT-THRESHOLD trigger for Stage 3 (breakeven +
trailing), as an ADDITIONAL path alongside the existing 2-favorable-
candle trigger -- whichever condition is met FIRST activates Stage 3.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_profit_threshold_trigger.py
"""

import importlib.util
import os

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))
MARKETS_TO_CHECK = ["1HZ25V", "1HZ75V", "1HZ90V", "1HZ100V", "R_100"]
THRESHOLDS_TO_TEST = [0.05, 0.10, 0.15, 0.20, 0.30]

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulate_with_profit_trigger(bs, df, trend_series, profit_threshold_pct):
    import backtester_sr_engulfing as sre

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")

    all_levels = sre.identify_swing_levels(df, bs.SWING_LOOKBACK)
    level_becomes_known_at = {lvl["index"] + bs.SWING_LOOKBACK: lvl for lvl in all_levels}
    loop_start = max(bs.SWING_LOOKBACK * 2 + 2, bs.MAX_CANDLES_NEEDED + 1)

    active_levels = [
        dict(lvl, tested_count=0)
        for known_at, lvl in level_becomes_known_at.items()
        if known_at < loop_start
    ]

    trades = []
    open_trade = None
    open_trade_index = None
    stop_loss_price = None
    favorable_candle_count = 0
    stage3_active = False

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= bs.LEVEL_AGE_CAP]

        if open_trade is not None:
            candles_held = i - open_trade_index
            should_close = False
            exit_level = None

            profit_threshold_triggered_this_candle = False
            if not stage3_active and profit_threshold_pct is not None:
                if open_trade["direction"] == "BUY":
                    current_favorable_price = current_high
                    current_profit_pct = (
                        (current_favorable_price - open_trade["entry_price"]) / open_trade["entry_price"] * 100
                    )
                else:
                    current_favorable_price = current_low
                    current_profit_pct = (
                        (open_trade["entry_price"] - current_favorable_price) / open_trade["entry_price"] * 100
                    )

                if current_profit_pct >= profit_threshold_pct:
                    profit_threshold_triggered_this_candle = True

            if stage3_active:
                window_start = max(0, i - bs.STAGE3_WINDOW)
                if open_trade["direction"] == "BUY":
                    new_stop = min(lows[window_start:i]) if window_start < i else stop_loss_price
                    if new_stop > stop_loss_price:
                        stop_loss_price = new_stop
                    should_close = current_low <= stop_loss_price
                else:
                    new_stop = max(highs[window_start:i]) if window_start < i else stop_loss_price
                    if new_stop < stop_loss_price:
                        stop_loss_price = new_stop
                    should_close = current_high >= stop_loss_price
                if should_close:
                    exit_level = stop_loss_price
            else:
                if open_trade["direction"] == "BUY":
                    hit_stop = current_low <= stop_loss_price
                else:
                    hit_stop = current_high >= stop_loss_price
                if hit_stop:
                    should_close = True
                    exit_level = stop_loss_price

                favorable = (
                    current_close > current_open if open_trade["direction"] == "BUY"
                    else current_close < current_open
                )
                if favorable:
                    favorable_candle_count += 1
                    if favorable_candle_count >= bs.NUM_FAVORABLE_CANDLES_REQUIRED:
                        stop_loss_price = open_trade["entry_price"]
                        stage3_active = True
                else:
                    favorable_candle_count = 0

                if profit_threshold_triggered_this_candle and not stage3_active:
                    stop_loss_price = open_trade["entry_price"]
                    stage3_active = True

            if should_close:
                exit_price = sre.exit_fill_price(exit_level, open_trade["direction"])
                entry_price = open_trade["entry_price"]
                if open_trade["direction"] == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100

                trades.append({"pct_change": pct_change, "initial_stop_distance_pct": open_trade["initial_stop_distance_pct"]})
                open_trade = None
                open_trade_index = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False

            continue

        if trend not in ("UP", "DOWN"):
            continue

        direction = "BUY" if trend == "UP" else "SELL"
        required_level_type = "support" if direction == "BUY" else "resistance"

        for level in active_levels:
            if level["type"] != required_level_type:
                continue

            level_price = level["price"]
            tolerance = level_price * (bs.RETEST_TOLERANCE_PCT / 100)
            retest_price = lows[i - 1] if level["type"] == "support" else highs[i - 1]
            if not (level_price - tolerance <= retest_price <= level_price + tolerance):
                continue

            level["tested_count"] += 1

            recent_candles = [
                bs.Candle(open=opens[j], high=highs[j], low=lows[j], close=closes[j])
                for j in range(i - bs.MAX_CANDLES_NEEDED + 1, i + 1)
            ]
            matched_pattern = bs.any_pattern_matches(direction, recent_candles)
            if matched_pattern is None:
                continue

            fill_price = sre.entry_fill_price(current_close, direction)
            window_start = max(0, i - bs.TRAILING_WINDOW + 1)
            initial_stop = (
                min(lows[window_start: i + 1]) if direction == "BUY"
                else max(highs[window_start: i + 1])
            )
            stop_distance_pct = abs(fill_price - initial_stop) / fill_price * 100

            open_trade = {
                "direction": direction, "entry_price": fill_price,
                "initial_stop_distance_pct": stop_distance_pct,
            }
            open_trade_index = i
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
        print("=" * 90)
        print(f"MARKET: {symbol}")
        print("=" * 90)

        try:
            bs = load_market_module(symbol)
            candles = bs.load_candles()
            trend_series = bs.build_trend_series(candles)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIPPED: {exc!r}\n")
            continue

        baseline_trades = simulate_with_profit_trigger(bs, candles, trend_series, None)
        baseline_balance = compound(baseline_trades, STARTING_BALANCE, RISK_PCT)
        baseline_ret = (baseline_balance / STARTING_BALANCE - 1) * 100

        print(f"  {'Variant':>20} | {'Trades':>7} | {'Win Rate':>9} | {'Compounded Return':>18}")
        print("  " + "-" * 62)

        wins = sum(1 for t in baseline_trades if t["pct_change"] > 0)
        win_rate = wins / len(baseline_trades) * 100 if baseline_trades else 0
        print(f"  {'BASELINE (no change)':>20} | {len(baseline_trades):>7} | {win_rate:>8.1f}% | {baseline_ret:>+17.1f}%")

        for threshold in THRESHOLDS_TO_TEST:
            trades = simulate_with_profit_trigger(bs, candles, trend_series, threshold)
            balance = compound(trades, STARTING_BALANCE, RISK_PCT)
            ret = (balance / STARTING_BALANCE - 1) * 100
            wins = sum(1 for t in trades if t["pct_change"] > 0)
            win_rate = wins / len(trades) * 100 if trades else 0
            label = f"threshold={threshold}%"
            print(f"  {label:>20} | {len(trades):>7} | {win_rate:>8.1f}% | {ret:>+17.1f}%")

        print()


if __name__ == "__main__":
    main()
