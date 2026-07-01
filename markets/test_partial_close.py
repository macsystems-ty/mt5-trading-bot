"""
test_partial_close.py

Tests PARTIAL POSITION CLOSING: when a trade's unrealized profit
reaches a given threshold, close HALF the position immediately
(banking real, partial profit), while the remaining half continues
under the EXISTING, unmodified exit mechanism.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_partial_close.py
"""

import importlib.util
import os

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))
MARKETS_TO_CHECK = ["1HZ25V", "1HZ75V", "1HZ90V", "1HZ100V", "R_100"]
PARTIAL_CLOSE_THRESHOLDS = [0.1, 0.2, 0.3, 0.5]
PARTIAL_CLOSE_FRACTION = 0.5

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulate_with_partial_close(bs, df, trend_series, partial_threshold_pct):
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
    partial_closed = False
    partial_close_pct_change = None

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= bs.LEVEL_AGE_CAP]

        if open_trade is not None:
            candles_held = i - open_trade_index

            if not partial_closed and partial_threshold_pct is not None:
                if open_trade["direction"] == "BUY":
                    current_profit_pct = (
                        (current_high - open_trade["entry_price"]) / open_trade["entry_price"] * 100
                    )
                else:
                    current_profit_pct = (
                        (open_trade["entry_price"] - current_low) / open_trade["entry_price"] * 100
                    )

                if current_profit_pct >= partial_threshold_pct:
                    partial_closed = True
                    partial_close_pct_change = partial_threshold_pct

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

            if should_close:
                exit_price = sre.exit_fill_price(exit_level, open_trade["direction"])
                entry_price = open_trade["entry_price"]
                if open_trade["direction"] == "BUY":
                    remainder_pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    remainder_pct_change = (entry_price - exit_price) / entry_price * 100

                if partial_closed:
                    trades.append({
                        "pct_change": partial_close_pct_change,
                        "initial_stop_distance_pct": open_trade["initial_stop_distance_pct"],
                        "size_fraction": PARTIAL_CLOSE_FRACTION,
                    })
                    trades.append({
                        "pct_change": remainder_pct_change,
                        "initial_stop_distance_pct": open_trade["initial_stop_distance_pct"],
                        "size_fraction": 1 - PARTIAL_CLOSE_FRACTION,
                    })
                else:
                    trades.append({
                        "pct_change": remainder_pct_change,
                        "initial_stop_distance_pct": open_trade["initial_stop_distance_pct"],
                        "size_fraction": 1.0,
                    })

                open_trade = None
                open_trade_index = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False
                partial_closed = False
                partial_close_pct_change = None

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
            partial_closed = False
            partial_close_pct_change = None
            break

    return trades


def compound(trades, starting_balance, risk_pct):
    balance = starting_balance
    for t in trades:
        if not t["initial_stop_distance_pct"]:
            continue
        size_fraction = t.get("size_fraction", 1.0)
        risk_dollars = balance * (risk_pct / 100) * size_fraction
        position_value = risk_dollars / (t["initial_stop_distance_pct"] / 100)
        balance += position_value * (t["pct_change"] / 100)
        if balance <= 0:
            balance = 0
    return balance


def compound_windowed(trades, window_size, starting_balance, risk_pct):
    results = []
    for start in range(0, len(trades), window_size):
        chunk = trades[start:start + window_size]
        if not chunk:
            continue
        balance = compound(chunk, starting_balance, risk_pct)
        ret_pct = (balance / starting_balance - 1) * 100
        results.append({"trades": len(chunk), "return_pct": ret_pct})
    return results


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

        baseline_trades = simulate_with_partial_close(bs, candles, trend_series, None)
        baseline_balance = compound(baseline_trades, STARTING_BALANCE, RISK_PCT)
        baseline_ret = (baseline_balance / STARTING_BALANCE - 1) * 100

        print(f"  {'Variant':>25} | {'Trades':>7} | {'Compounded Return':>18}")
        print("  " + "-" * 58)
        print(f"  {'BASELINE (no change)':>25} | {len(baseline_trades):>7} | {baseline_ret:>+17.1f}%")

        for threshold in PARTIAL_CLOSE_THRESHOLDS:
            trades = simulate_with_partial_close(bs, candles, trend_series, threshold)
            balance = compound(trades, STARTING_BALANCE, RISK_PCT)
            ret = (balance / STARTING_BALANCE - 1) * 100
            label = f"partial_close={threshold}%"
            print(f"  {label:>25} | {len(trades):>7} | {ret:>+17.1f}%")

        print(f"\n  --- LAST 100 ORIGINAL TRADES (by entry count), 20-trade windows ---")
        baseline_last100 = baseline_trades[-100:] if len(baseline_trades) >= 100 else baseline_trades
        baseline_windows = compound_windowed(baseline_last100, 20, STARTING_BALANCE, RISK_PCT)
        print(f"    {'Variant':>25} | " + " | ".join(f"W{i+1:>6}" for i in range(len(baseline_windows))))
        row = f"    {'BASELINE':>25} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in baseline_windows)
        print(row)

        for threshold in PARTIAL_CLOSE_THRESHOLDS:
            trades = simulate_with_partial_close(bs, candles, trend_series, threshold)
            last100 = trades[-100:] if len(trades) >= 100 else trades
            windows = compound_windowed(last100, 20, STARTING_BALANCE, RISK_PCT)
            label = f"partial_close={threshold}%"
            row = f"    {label:>25} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in windows)
            print(row)

        print()


if __name__ == "__main__":
    main()
