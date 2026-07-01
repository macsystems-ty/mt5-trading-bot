"""
check_losses_were_profitable.py

For every LOSING trade in the last 500 trades of a market's
validated strategy, checks whether it was ever genuinely in PROFIT
at some point before reversing into a final loss.

Run this FROM INSIDE the specific market folder you want to check:
    cd markets/1HZ90V
    python ../check_losses_were_profitable.py
"""

import os
import sys

sys.path.insert(0, os.getcwd())

import backtest_strategy as bs  # noqa: E402

NUM_TRADES_TO_CHECK = 500


def is_favorable_candle(open_price, close_price, direction):
    if direction == "BUY":
        return close_price > open_price
    return close_price < open_price


def simulate_with_profit_tracking(df, trend_series):
    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")

    import backtester_sr_engulfing as sre

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
    max_favorable_price = None

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= bs.LEVEL_AGE_CAP]

        if open_trade is not None:
            candles_held = i - open_trade_index

            if open_trade["direction"] == "BUY":
                if max_favorable_price is None or current_high > max_favorable_price:
                    max_favorable_price = current_high
            else:
                if max_favorable_price is None or current_low < max_favorable_price:
                    max_favorable_price = current_low

            should_close = False
            exit_level = None

            if stage3_active:
                window_start = max(0, i - bs.STAGE3_WINDOW)
                if open_trade["direction"] == "BUY":
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
                if open_trade["direction"] == "BUY":
                    hit_stop = current_low <= stop_loss_price
                else:
                    hit_stop = current_high >= stop_loss_price
                if hit_stop:
                    should_close = True
                    exit_level = stop_loss_price

                favorable = is_favorable_candle(current_open, current_close, open_trade["direction"])
                if favorable:
                    favorable_candle_count += 1
                    if favorable_candle_count >= bs.NUM_FAVORABLE_CANDLES_REQUIRED:
                        stop_loss_price = open_trade["entry_price"]
                        stage3_active = True
                else:
                    favorable_candle_count = 0

            if should_close:
                exit_price = sre.exit_fill_price(exit_level, open_trade["direction"])
                entry_price = open_trade["entry_price"]
                if open_trade["direction"] == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price * 100
                    max_favorable_pct = (
                        (max_favorable_price - entry_price) / entry_price * 100
                        if max_favorable_price is not None else 0
                    )
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100
                    max_favorable_pct = (
                        (entry_price - max_favorable_price) / entry_price * 100
                        if max_favorable_price is not None else 0
                    )

                trades.append({
                    "direction": open_trade["direction"],
                    "entry_time": open_trade["entry_time"],
                    "entry_price": entry_price,
                    "pct_change": pct_change,
                    "max_favorable_pct": max(0.0, max_favorable_pct),
                    "candles_held": candles_held,
                })
                open_trade = None
                open_trade_index = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False
                max_favorable_price = None

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

            open_trade = {"direction": direction, "entry_time": current_time, "entry_price": fill_price}
            open_trade_index = i
            stop_loss_price = initial_stop
            favorable_candle_count = 0
            stage3_active = False
            max_favorable_price = None
            break

    return trades


def main() -> None:
    print(f"Loading 5min candles for {bs.SYMBOL} ...")
    candles = bs.load_candles()
    trend_series = bs.build_trend_series(candles)

    print("Running simulation with profit tracking ...\n")
    trades = simulate_with_profit_tracking(candles, trend_series)

    last_n = trades[-NUM_TRADES_TO_CHECK:] if len(trades) >= NUM_TRADES_TO_CHECK else trades
    losses = [t for t in last_n if t["pct_change"] <= 0]
    wins = [t for t in last_n if t["pct_change"] > 0]

    print("=" * 70)
    print(f"RESULTS for {bs.SYMBOL} (last {len(last_n)} trades)")
    print("=" * 70)
    print(f"Total trades checked: {len(last_n)}")
    print(f"Wins: {len(wins)} | Losses: {len(losses)}\n")

    was_profitable_at_some_point = [t for t in losses if t["max_favorable_pct"] > 0.001]
    never_profitable = [t for t in losses if t["max_favorable_pct"] <= 0.001]

    if losses:
        print(f"Of {len(losses)} losing trades:")
        print(
            f"  WAS in profit at some point before reversing: {len(was_profitable_at_some_point)} "
            f"({len(was_profitable_at_some_point)/len(losses)*100:.1f}% of losses)"
        )
        print(
            f"  NEVER showed any profit at all: {len(never_profitable)} "
            f"({len(never_profitable)/len(losses)*100:.1f}% of losses)"
        )
    else:
        print("No losing trades in this window.")

    if was_profitable_at_some_point:
        avg_peak_profit = sum(t["max_favorable_pct"] for t in was_profitable_at_some_point) / len(was_profitable_at_some_point)
        max_peak_profit = max(t["max_favorable_pct"] for t in was_profitable_at_some_point)
        print(f"\n  Average peak profit reached before reversing: {avg_peak_profit:.4f}%")
        print(f"  Largest peak profit reached before reversing: {max_peak_profit:.4f}%")

        print(f"\n  Top 10 biggest 'gave-back' trades (highest peak profit before ending in loss):")
        top_giveback = sorted(was_profitable_at_some_point, key=lambda t: -t["max_favorable_pct"])[:10]
        print(f"  {'Entry Time':>26} | {'Dir':>4} | {'Peak Profit':>12} | {'Final Result':>13} | {'Candles Held':>12}")
        print("  " + "-" * 78)
        for t in top_giveback:
            print(
                f"  {str(t['entry_time']):>26} | {t['direction']:>4} | {t['max_favorable_pct']:>+11.4f}% | "
                f"{t['pct_change']:>+12.4f}% | {t['candles_held']:>12}"
            )


if __name__ == "__main__":
    main()
