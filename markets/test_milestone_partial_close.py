"""
test_milestone_partial_close.py

Tests LARGE-MILESTONE partial closing: banks HALF of whatever
remains of the position, but ONLY when a trade reaches a truly
outlier-sized profit (5x or 10x the original risk amount).

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_milestone_partial_close.py
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

MILESTONE_MULTS_TO_TEST = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
MILESTONE_CLOSE_FRACTION = 0.5

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def simulate_with_milestone_close(bs, df, trend_series, fixed_stop_pct, max_trend_strength_pct, milestone_mult):
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

    completed_chunks = []
    open_trade = None
    stop_loss_price = None
    favorable_candle_count = 0
    stage3_active = False

    remaining_fraction = 1.0
    milestone_already_hit = False

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
            should_close_remainder = False
            exit_level = None

            if milestone_mult is not None and not milestone_already_hit:
                if direction == "BUY":
                    current_pct = (current_high - entry_price) / entry_price * 100
                else:
                    current_pct = (entry_price - current_low) / entry_price * 100
                current_risk_multiple = current_pct / fixed_stop_pct if fixed_stop_pct else 0

                if current_risk_multiple >= milestone_mult:
                    milestone_already_hit = True
                    bank_fraction = remaining_fraction * MILESTONE_CLOSE_FRACTION
                    exit_price_partial = sre.exit_fill_price(current_close, direction)
                    if direction == "BUY":
                        pct_change = (exit_price_partial - entry_price) / entry_price * 100
                    else:
                        pct_change = (entry_price - exit_price_partial) / entry_price * 100
                    completed_chunks.append({
                        "pct_change": pct_change,
                        "size_fraction": bank_fraction,
                        "initial_stop_distance_pct": fixed_stop_pct,
                    })
                    remaining_fraction -= bank_fraction

            if stage3_active:
                window_start = max(0, i - bs.STAGE3_WINDOW)
                if direction == "BUY":
                    new_stop = min(lows[window_start:i])
                    if new_stop > stop_loss_price:
                        stop_loss_price = new_stop
                    should_close_remainder = current_low <= stop_loss_price
                else:
                    new_stop = max(highs[window_start:i])
                    if new_stop < stop_loss_price:
                        stop_loss_price = new_stop
                    should_close_remainder = current_high >= stop_loss_price
                if should_close_remainder:
                    exit_level = stop_loss_price
            else:
                if direction == "BUY":
                    hit_stop = current_low <= stop_loss_price
                else:
                    hit_stop = current_high >= stop_loss_price
                if hit_stop:
                    should_close_remainder = True
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

            if should_close_remainder:
                exit_price = sre.exit_fill_price(exit_level, direction)
                if direction == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100

                completed_chunks.append({
                    "pct_change": pct_change,
                    "size_fraction": remaining_fraction,
                    "initial_stop_distance_pct": fixed_stop_pct,
                })

                open_trade = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False
                remaining_fraction = 1.0
                milestone_already_hit = False

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

            open_trade = {"direction": direction, "entry_price": fill_price}
            stop_loss_price = initial_stop
            favorable_candle_count = 0
            stage3_active = False
            remaining_fraction = 1.0
            milestone_already_hit = False
            break

    return completed_chunks


def compound(chunks, starting_balance, risk_pct):
    balance = starting_balance
    for c in chunks:
        if not c["initial_stop_distance_pct"]:
            continue
        risk_dollars = balance * (risk_pct / 100) * c["size_fraction"]
        position_value = risk_dollars / (c["initial_stop_distance_pct"] / 100)
        balance += position_value * (c["pct_change"] / 100)
        if balance <= 0:
            balance = 0
    return balance


def compound_windowed(chunks, window_size, starting_balance, risk_pct):
    results = []
    for start in range(0, len(chunks), window_size):
        chunk_group = chunks[start:start + window_size]
        if not chunk_group:
            continue
        balance = compound(chunk_group, starting_balance, risk_pct)
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

        baseline_chunks = simulate_with_milestone_close(bs, candles, trend_series, fixed_pct, max_strength, None)
        baseline_balance = compound(baseline_chunks, STARTING_BALANCE, RISK_PCT)
        baseline_ret = (baseline_balance / STARTING_BALANCE - 1) * 100

        print(f"  {'Variant':>30} | {'Chunks':>7} | {'Compounded Return':>18}")
        print("  " + "-" * 65)
        print(f"  {'BASELINE (no milestone)':>30} | {len(baseline_chunks):>7} | {baseline_ret:>+17.1f}%")

        results = {}
        for milestone_mult in MILESTONE_MULTS_TO_TEST:
            chunks = simulate_with_milestone_close(bs, candles, trend_series, fixed_pct, max_strength, milestone_mult)
            balance = compound(chunks, STARTING_BALANCE, RISK_PCT)
            ret = (balance / STARTING_BALANCE - 1) * 100
            milestones_hit = sum(1 for c in chunks if 0 < c["size_fraction"] < 1.0)
            label = f"milestone={milestone_mult}x"
            print(f"  {label:>30} | {len(chunks):>7} | {ret:>+17.1f}%  (milestones hit: {milestones_hit})")
            results[label] = chunks

        print(f"\n  --- LAST 100 closed chunks, 20-chunk windows ---")
        baseline_last100 = baseline_chunks[-100:] if len(baseline_chunks) >= 100 else baseline_chunks
        baseline_windows = compound_windowed(baseline_last100, 20, STARTING_BALANCE, RISK_PCT)
        print(f"    {'Variant':>20} | " + " | ".join(f"W{i+1:>6}" for i in range(len(baseline_windows))) + " | Wins-vs-BL")
        print(f"    {'BASELINE':>20} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in baseline_windows) + " |     N/A")

        for label, chunks in results.items():
            last100 = chunks[-100:] if len(chunks) >= 100 else chunks
            windows = compound_windowed(last100, 20, STARTING_BALANCE, RISK_PCT)
            wins = sum(1 for bw, w in zip(baseline_windows, windows) if w["return_pct"] > bw["return_pct"])
            row = f"    {label:>20} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in windows) + f" |     {wins}/{len(baseline_windows)}"
            print(row)

        print()


if __name__ == "__main__":
    main()