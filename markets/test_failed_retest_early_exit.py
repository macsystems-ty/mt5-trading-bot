"""
test_failed_retest_early_exit.py

Tests an EARLY EXIT rule based on a "failed retest" pattern:
  1. Trade dips negative (any amount below entry) at some point.
  2. Price recovers back to within RETEST_TOLERANCE_MULT x the
     market's fixed stop distance of entry (the "retest").
  3. After the retest, price turns down again by at least
     REVERSAL_TRIGGER_MULT x the stop distance from the retest's
     high point -- close IMMEDIATELY here, rather than waiting for
     the normal stop to be hit.

Based on the user's example: BUY @ 20000, SL @ 19000 (5% stop),
dipped to 19500 (2.5%), retested to 19900-20000 (within 0.5%), then
turned down again to 19850 (0.75% off the retest high) -- close
THERE instead of riding it down to the full 19000 stop.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_failed_retest_early_exit.py
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

RETEST_TOLERANCE_MULTS = [0.5, 1.0]
REVERSAL_TRIGGER_MULTS = [0.1, 0.15, 0.2]

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulate_with_failed_retest_exit(bs, df, trend_series, fixed_stop_pct, retest_tolerance_mult, reversal_trigger_mult):
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

    has_dipped_negative = False
    has_retested = False
    retest_high_water_mark = None

    for i in range(loop_start, len(df)):
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= bs.LEVEL_AGE_CAP]

        if open_trade is not None:
            should_close = False
            exit_level = None
            exit_reason = None

            entry_price = open_trade["entry_price"]
            direction = open_trade["direction"]

            if direction == "BUY":
                current_pct = (current_close - entry_price) / entry_price * 100
            else:
                current_pct = (entry_price - current_close) / entry_price * 100

            if retest_tolerance_mult is not None:
                retest_tolerance_pct = fixed_stop_pct * retest_tolerance_mult
                reversal_trigger_pct = fixed_stop_pct * reversal_trigger_mult

                if not has_dipped_negative and current_pct < 0:
                    has_dipped_negative = True

                if has_dipped_negative and not has_retested:
                    if current_pct >= -retest_tolerance_pct:
                        has_retested = True
                        retest_high_water_mark = current_pct

                if has_retested:
                    if current_pct > retest_high_water_mark:
                        retest_high_water_mark = current_pct
                    drop_from_retest_high = retest_high_water_mark - current_pct
                    if drop_from_retest_high >= reversal_trigger_pct and current_pct < 0:
                        should_close = True
                        exit_reason = "failed_retest_early_exit"

            if should_close:
                pct_change = current_pct
            else:
                if stage3_active:
                    window_start = max(0, i - bs.STAGE3_WINDOW)
                    if direction == "BUY":
                        new_stop = min(lows[window_start:i])
                        if new_stop > stop_loss_price:
                            stop_loss_price = new_stop
                        hit = current_low <= stop_loss_price
                    else:
                        new_stop = max(highs[window_start:i])
                        if new_stop < stop_loss_price:
                            stop_loss_price = new_stop
                        hit = current_high >= stop_loss_price
                    if hit:
                        should_close = True
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

                if should_close and exit_level is not None:
                    exit_price = sre.exit_fill_price(exit_level, direction)
                    if direction == "BUY":
                        pct_change = (exit_price - entry_price) / entry_price * 100
                    else:
                        pct_change = (entry_price - exit_price) / entry_price * 100
                else:
                    pct_change = None

            if should_close:
                trades.append({
                    "pct_change": pct_change,
                    "initial_stop_distance_pct": open_trade["initial_stop_distance_pct"],
                    "exit_reason": exit_reason or "normal",
                })
                open_trade = None
                open_trade_index = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False
                has_dipped_negative = False
                has_retested = False
                retest_high_water_mark = None

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

            if direction == "BUY":
                initial_stop = fill_price * (1 - fixed_stop_pct / 100)
            else:
                initial_stop = fill_price * (1 + fixed_stop_pct / 100)

            open_trade = {"direction": direction, "entry_price": fill_price, "initial_stop_distance_pct": fixed_stop_pct}
            open_trade_index = i
            stop_loss_price = initial_stop
            favorable_candle_count = 0
            stage3_active = False
            has_dipped_negative = False
            has_retested = False
            retest_high_water_mark = None
            break

    return trades


def compound(trades, starting_balance, risk_pct):
    balance = starting_balance
    for t in trades:
        if t["pct_change"] is None or not t["initial_stop_distance_pct"]:
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t["initial_stop_distance_pct"] / 100)
        balance += position_value * (t["pct_change"] / 100)
        if balance <= 0:
            balance = 0
    return balance


def compound_windowed(trades, window_size, starting_balance, risk_pct):
    valid = [t for t in trades if t["pct_change"] is not None]
    results = []
    for start in range(0, len(valid), window_size):
        chunk = valid[start:start + window_size]
        if not chunk:
            continue
        balance = compound(chunk, starting_balance, risk_pct)
        ret_pct = (balance / starting_balance - 1) * 100
        results.append({"trades": len(chunk), "return_pct": ret_pct})
    return results


def main() -> None:
    for symbol in MARKETS_TO_CHECK:
        print("=" * 90)
        print(f"MARKET: {symbol}  (fixed stop: {FIXED_STOP_PCT[symbol]}%)")
        print("=" * 90)

        try:
            bs = load_market_module(symbol)
            candles = bs.load_candles()
            trend_series = bs.build_trend_series(candles)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIPPED: {exc!r}\n")
            continue

        fixed_pct = FIXED_STOP_PCT[symbol]

        baseline_trades = simulate_with_failed_retest_exit(bs, candles, trend_series, fixed_pct, None, None)
        baseline_balance = compound(baseline_trades, STARTING_BALANCE, RISK_PCT)
        baseline_ret = (baseline_balance / STARTING_BALANCE - 1) * 100

        print(f"  {'Variant':>30} | {'Trades':>7} | {'Early Exits':>11} | {'Compounded Return':>18}")
        print("  " + "-" * 75)
        print(f"  {'BASELINE (fixed stop only)':>30} | {len(baseline_trades):>7} | {'N/A':>11} | {baseline_ret:>+17.1f}%")

        results_for_windows = {}
        for tol_mult in RETEST_TOLERANCE_MULTS:
            for rev_mult in REVERSAL_TRIGGER_MULTS:
                trades = simulate_with_failed_retest_exit(bs, candles, trend_series, fixed_pct, tol_mult, rev_mult)
                balance = compound(trades, STARTING_BALANCE, RISK_PCT)
                ret = (balance / STARTING_BALANCE - 1) * 100
                early_exits = sum(1 for t in trades if t["exit_reason"] == "failed_retest_early_exit")
                label = f"tol={tol_mult}x,trig={rev_mult}x"
                print(f"  {label:>30} | {len(trades):>7} | {early_exits:>11} | {ret:>+17.1f}%")
                results_for_windows[label] = trades

        print(f"\n  --- LAST 100 TRADES, 20-trade windows ---")
        baseline_last100 = baseline_trades[-100:] if len(baseline_trades) >= 100 else baseline_trades
        baseline_windows = compound_windowed(baseline_last100, 20, STARTING_BALANCE, RISK_PCT)
        print(f"    {'Variant':>30} | " + " | ".join(f"W{i+1:>6}" for i in range(len(baseline_windows))) + " | Wins-vs-BL")
        row = f"    {'BASELINE':>30} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in baseline_windows) + " |     N/A"
        print(row)

        for label, trades in results_for_windows.items():
            last100 = trades[-100:] if len(trades) >= 100 else trades
            windows = compound_windowed(last100, 20, STARTING_BALANCE, RISK_PCT)
            wins_vs_baseline = sum(
                1 for bw, w in zip(baseline_windows, windows) if w["return_pct"] > bw["return_pct"]
            )
            row = (
                f"    {label:>30} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in windows)
                + f" |     {wins_vs_baseline}/{len(baseline_windows)}"
            )
            print(row)

        print()


if __name__ == "__main__":
    main()
