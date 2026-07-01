"""
test_profit_floor_trailing.py

Tests a PROFIT-FLOOR trailing stop, running ALONGSIDE (never
replacing) the existing candle-based Stage 3 trailing:

  1. Track each trade's peak unrealized profit in RISK MULTIPLES.
  2. Once peak profit reaches TRIGGER_MULT (e.g. 2x risk), an
     INDEPENDENT floor activates: the stop must never let profit
     fall below FLOOR_FRACTION x peak_profit, regardless of what the
     candle-based trailing says.
  3. At every step, whichever stop (candle-based or floor-based) is
     TIGHTER is the one that actually applies.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_profit_floor_trailing.py
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
MAX_TREND_STRENGTH_PCT = {
    "1HZ25V": 0.2838,
    "1HZ75V": 0.7879,
    "1HZ90V": 0.9165,
    "1HZ100V": 1.0529,
    "R_100": 1.0407,
}

TRIGGER_MULT = 2.0
FLOOR_FRACTIONS_TO_TEST = [0.5, 0.65, 0.75, 0.85, 0.9, 0.95, 0.98]

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulate_with_profit_floor(bs, df, trend_series, fixed_stop_pct, max_trend_strength_pct, floor_fraction):
    import backtester_sr_engulfing as sre

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")
    ema_full = bs.indicators.add_all_indicators(
        df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    )["ema_14"]

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
    stop_loss_price = None
    favorable_candle_count = 0
    stage3_active = False

    peak_risk_multiple = 0.0
    floor_active = False

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= bs.LEVEL_AGE_CAP]

        if open_trade is not None:
            direction = open_trade["direction"]
            entry_price = open_trade["entry_price"]
            risk_distance_pct = open_trade["risk_distance_pct"]
            should_close = False
            exit_level = None
            exit_via_floor = False

            if direction == "BUY":
                current_pct = (current_high - entry_price) / entry_price * 100
            else:
                current_pct = (entry_price - current_low) / entry_price * 100
            current_risk_multiple = current_pct / risk_distance_pct if risk_distance_pct else 0

            if current_risk_multiple > peak_risk_multiple:
                peak_risk_multiple = current_risk_multiple

            if floor_fraction is not None and not floor_active and peak_risk_multiple >= TRIGGER_MULT:
                floor_active = True

            candle_stop_price = None
            if stage3_active:
                window_start = max(0, i - bs.STAGE3_WINDOW)
                if direction == "BUY":
                    new_stop = min(lows[window_start:i])
                    if new_stop > stop_loss_price:
                        stop_loss_price = new_stop
                    candle_stop_price = stop_loss_price
                else:
                    new_stop = max(highs[window_start:i])
                    if new_stop < stop_loss_price:
                        stop_loss_price = new_stop
                    candle_stop_price = stop_loss_price
            else:
                candle_stop_price = stop_loss_price

            floor_stop_price = None
            if floor_active:
                floor_profit_pct = floor_fraction * peak_risk_multiple * risk_distance_pct
                if direction == "BUY":
                    floor_stop_price = entry_price * (1 + floor_profit_pct / 100)
                else:
                    floor_stop_price = entry_price * (1 - floor_profit_pct / 100)

            if direction == "BUY":
                if candle_stop_price is None:
                    effective_stop = floor_stop_price
                elif floor_stop_price is None:
                    effective_stop = candle_stop_price
                else:
                    effective_stop = max(candle_stop_price, floor_stop_price)
                should_close = current_low <= effective_stop if effective_stop is not None else False
                exit_via_floor = (
                    floor_stop_price is not None
                    and effective_stop == floor_stop_price
                    and floor_stop_price > (candle_stop_price if candle_stop_price is not None else -float("inf"))
                )
            else:
                if candle_stop_price is None:
                    effective_stop = floor_stop_price
                elif floor_stop_price is None:
                    effective_stop = candle_stop_price
                else:
                    effective_stop = min(candle_stop_price, floor_stop_price)
                should_close = current_high >= effective_stop if effective_stop is not None else False
                exit_via_floor = (
                    floor_stop_price is not None
                    and effective_stop == floor_stop_price
                    and floor_stop_price < (candle_stop_price if candle_stop_price is not None else float("inf"))
                )

            if should_close:
                exit_level = effective_stop
            elif not stage3_active:
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

                trades.append({
                    "pct_change": pct_change,
                    "initial_stop_distance_pct": fixed_stop_pct,
                    "via_floor": exit_via_floor,
                })

                open_trade = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False
                peak_risk_multiple = 0.0
                floor_active = False

            continue

        if trend not in ("UP", "DOWN"):
            continue

        direction = "BUY" if trend == "UP" else "SELL"
        required_level_type = "support" if direction == "BUY" else "resistance"

        ema_at_entry = ema_full.asof(current_time)
        if ema_at_entry is None or ema_at_entry == 0:
            continue
        trend_strength_pct = abs(current_close - ema_at_entry) / ema_at_entry * 100
        if trend_strength_pct > max_trend_strength_pct:
            continue

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

            open_trade = {"direction": direction, "entry_price": fill_price, "risk_distance_pct": fixed_stop_pct}
            stop_loss_price = initial_stop
            favorable_candle_count = 0
            stage3_active = False
            peak_risk_multiple = 0.0
            floor_active = False
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


def compound_windowed(trades, window_size, starting_balance, risk_pct):
    results = []
    for start in range(0, len(trades), window_size):
        chunk = trades[start:start + window_size]
        if not chunk:
            continue
        balance = compound(chunk, starting_balance, risk_pct)
        ret_pct = (balance / starting_balance - 1) * 100
        results.append({"return_pct": ret_pct})
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

        fixed_pct = FIXED_STOP_PCT[symbol]
        max_strength = MAX_TREND_STRENGTH_PCT[symbol]

        baseline_trades = simulate_with_profit_floor(bs, candles, trend_series, fixed_pct, max_strength, None)
        baseline_balance = compound(baseline_trades, STARTING_BALANCE, RISK_PCT)
        baseline_ret = (baseline_balance / STARTING_BALANCE - 1) * 100

        print(f"  {'Variant':>30} | {'Trades':>7} | {'Via Floor':>9} | {'Compounded Return':>18}")
        print("  " + "-" * 75)
        print(f"  {'BASELINE (candle-only)':>30} | {len(baseline_trades):>7} | {'N/A':>9} | {baseline_ret:>+17.1f}%")

        results = {}
        for floor_fraction in FLOOR_FRACTIONS_TO_TEST:
            trades = simulate_with_profit_floor(bs, candles, trend_series, fixed_pct, max_strength, floor_fraction)
            balance = compound(trades, STARTING_BALANCE, RISK_PCT)
            ret = (balance / STARTING_BALANCE - 1) * 100
            via_floor = sum(1 for t in trades if t.get("via_floor"))
            label = f"floor={floor_fraction}"
            print(f"  {label:>30} | {len(trades):>7} | {via_floor:>9} | {ret:>+17.1f}%")
            results[label] = trades

        print(f"\n  --- LAST 100 TRADES, 20-trade windows ---")
        baseline_last100 = baseline_trades[-100:] if len(baseline_trades) >= 100 else baseline_trades
        baseline_windows = compound_windowed(baseline_last100, 20, STARTING_BALANCE, RISK_PCT)
        print(f"    {'Variant':>20} | " + " | ".join(f"W{i+1:>6}" for i in range(len(baseline_windows))) + " | Wins-vs-BL")
        print(f"    {'BASELINE':>20} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in baseline_windows) + " |     N/A")

        for label, trades in results.items():
            last100 = trades[-100:] if len(trades) >= 100 else trades
            windows = compound_windowed(last100, 20, STARTING_BALANCE, RISK_PCT)
            wins = sum(1 for bw, w in zip(baseline_windows, windows) if w["return_pct"] > bw["return_pct"])
            row = f"    {label:>20} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in windows) + f" |     {wins}/{len(baseline_windows)}"
            print(row)

        print()


if __name__ == "__main__":
    main()