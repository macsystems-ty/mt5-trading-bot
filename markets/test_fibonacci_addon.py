"""
test_fibonacci_addon.py

Tests the FIBONACCI ADD-ON strategy:
  1. Original trade is managed EXACTLY as our validated baseline.
  2. Once in profit, track peak favorable price, compute Fib levels
     (23.6%, 38.2%, 50%, 61.8%) between entry and that peak.
  3. When price pulls back to a Fib level AND a candlestick pattern
     confirms continuation, open a NEW half-size position with its
     own candle-low/high stop and 1-candle breakeven.
  4. Max 2 add-on positions per original trade.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_fibonacci_addon.py
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

FIB_LEVELS = [0.236, 0.382, 0.5, 0.618]
MAX_ADDONS = 2
ADDON_SIZE_FRACTION = 0.5
ADDON_BREAKEVEN_CANDLES = 1

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Position:
    def __init__(self, direction, entry_price, stop_price, breakeven_candles_required, size_fraction, is_addon=False):
        self.direction = direction
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.breakeven_candles_required = breakeven_candles_required
        self.size_fraction = size_fraction
        self.is_addon = is_addon
        self.favorable_candle_count = 0
        self.stage3_active = False
        self.closed = False
        self.pct_change = None


def simulate_with_fib_addons(bs, df, trend_series, fixed_stop_pct, max_trend_strength_pct, enable_addons, max_addons=MAX_ADDONS, eligible_fib_levels=None):
    if eligible_fib_levels is None:
        eligible_fib_levels = FIB_LEVELS
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

    completed_trades = []
    group = None

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= bs.LEVEL_AGE_CAP]

        if group is not None:
            direction = group["direction"]

            if direction == "BUY":
                if current_high > group["peak"]:
                    group["peak"] = current_high
            else:
                if current_low < group["peak"]:
                    group["peak"] = current_low

            for pos in group["positions"]:
                if pos.closed:
                    continue

                should_close = False
                exit_level = None

                if pos.stage3_active:
                    window_start = max(0, i - bs.STAGE3_WINDOW)
                    if direction == "BUY":
                        new_stop = min(lows[window_start:i])
                        if new_stop > pos.stop_price:
                            pos.stop_price = new_stop
                        should_close = current_low <= pos.stop_price
                    else:
                        new_stop = max(highs[window_start:i])
                        if new_stop < pos.stop_price:
                            pos.stop_price = new_stop
                        should_close = current_high >= pos.stop_price
                    if should_close:
                        exit_level = pos.stop_price
                else:
                    if direction == "BUY":
                        hit_stop = current_low <= pos.stop_price
                    else:
                        hit_stop = current_high >= pos.stop_price
                    if hit_stop:
                        should_close = True
                        exit_level = pos.stop_price

                    favorable = (
                        current_close > current_open if direction == "BUY"
                        else current_close < current_open
                    )
                    if favorable:
                        pos.favorable_candle_count += 1
                        if pos.favorable_candle_count >= pos.breakeven_candles_required:
                            pos.stop_price = pos.entry_price
                            pos.stage3_active = True
                    else:
                        pos.favorable_candle_count = 0

                if should_close:
                    exit_price = sre.exit_fill_price(exit_level, direction)
                    if direction == "BUY":
                        pct_change = (exit_price - pos.entry_price) / pos.entry_price * 100
                    else:
                        pct_change = (pos.entry_price - exit_price) / pos.entry_price * 100
                    pos.pct_change = pct_change
                    pos.closed = True
                    completed_trades.append({"pct_change": pct_change, "size_fraction": pos.size_fraction})

            if enable_addons and not group["positions"][0].closed:
                num_addons_so_far = len(group["positions"]) - 1
                if num_addons_so_far < max_addons:
                    entry_price = group["entry_price"]
                    peak = group["peak"]
                    span = peak - entry_price if direction == "BUY" else entry_price - peak
                    if span > 0:
                        for fib_pct in eligible_fib_levels:
                            if fib_pct in group["fib_levels_used"]:
                                continue
                            if direction == "BUY":
                                fib_price = peak - span * fib_pct
                                touched = current_low <= fib_price <= current_high
                            else:
                                fib_price = peak + span * fib_pct
                                touched = current_low <= fib_price <= current_high
                            if not touched:
                                continue

                            recent_candles = [
                                bs.Candle(open=opens[j], high=highs[j], low=lows[j], close=closes[j])
                                for j in range(max(0, i - bs.MAX_CANDLES_NEEDED + 1), i + 1)
                            ]
                            if len(recent_candles) < bs.MAX_CANDLES_NEEDED:
                                continue
                            matched_pattern = bs.any_pattern_matches(direction, recent_candles)
                            if matched_pattern is None:
                                continue

                            addon_entry = sre.entry_fill_price(current_close, direction)
                            addon_stop = current_low if direction == "BUY" else current_high
                            new_pos = Position(
                                direction=direction, entry_price=addon_entry, stop_price=addon_stop,
                                breakeven_candles_required=ADDON_BREAKEVEN_CANDLES,
                                size_fraction=ADDON_SIZE_FRACTION, is_addon=True,
                            )
                            group["positions"].append(new_pos)
                            group["fib_levels_used"].add(fib_pct)
                            break

            if all(p.closed for p in group["positions"]):
                group = None

            continue

        if trend not in ("UP", "DOWN"):
            continue

        direction = "BUY" if trend == "UP" else "SELL"
        required_level_type = "support" if direction == "BUY" else "resistance"

        if max_trend_strength_pct is not None:
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

            original_pos = Position(
                direction=direction, entry_price=fill_price, stop_price=initial_stop,
                breakeven_candles_required=bs.NUM_FAVORABLE_CANDLES_REQUIRED,
                size_fraction=1.0, is_addon=False,
            )
            group = {
                "direction": direction, "entry_price": fill_price, "peak": fill_price,
                "positions": [original_pos], "fib_levels_used": set(),
            }
            break

    return completed_trades


def compound(trades, starting_balance, risk_pct, fixed_stop_pct):
    balance = starting_balance
    for t in trades:
        risk_dollars = balance * (risk_pct / 100) * t["size_fraction"]
        position_value = risk_dollars / (fixed_stop_pct / 100)
        balance += position_value * (t["pct_change"] / 100)
        if balance <= 0:
            balance = 0
    return balance


def compound_windowed(trades, window_size, starting_balance, risk_pct, fixed_stop_pct):
    results = []
    for start in range(0, len(trades), window_size):
        chunk = trades[start:start + window_size]
        if not chunk:
            continue
        balance = compound(chunk, starting_balance, risk_pct, fixed_stop_pct)
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

        baseline_trades = simulate_with_fib_addons(bs, candles, trend_series, fixed_pct, max_strength, enable_addons=False)

        variants = {
            "FULL (max 2, all levels)": dict(max_addons=2, eligible_fib_levels=FIB_LEVELS),
            "MAX 1 ADDON (all levels)": dict(max_addons=1, eligible_fib_levels=FIB_LEVELS),
            "DEEPEST ONLY (61.8%, max 2)": dict(max_addons=2, eligible_fib_levels=[0.618]),
        }

        results = {}
        for label, kwargs in variants.items():
            trades = simulate_with_fib_addons(
                bs, candles, trend_series, fixed_pct, max_strength, enable_addons=True, **kwargs
            )
            results[label] = trades

        baseline_balance = compound(baseline_trades, STARTING_BALANCE, RISK_PCT, fixed_pct)
        baseline_ret = (baseline_balance / STARTING_BALANCE - 1) * 100

        print(f"  {'Variant':>30} | {'Total Trades':>13} | {'(Originals/Add-ons)':>20} | {'Compounded Return':>18}")
        print("  " + "-" * 90)
        print(f"  {'BASELINE (no add-ons)':>30} | {len(baseline_trades):>13} | {'N/A':>20} | {baseline_ret:>+17.1f}%")

        for label, trades in results.items():
            balance = compound(trades, STARTING_BALANCE, RISK_PCT, fixed_pct)
            ret = (balance / STARTING_BALANCE - 1) * 100
            num_addons = sum(1 for t in trades if t["size_fraction"] < 1.0)
            num_originals = sum(1 for t in trades if t["size_fraction"] == 1.0)
            print(f"  {label:>30} | {len(trades):>13} | {f'{num_originals}/{num_addons}':>20} | {ret:>+17.1f}%")

        print(f"\n  --- LAST 100 TRADES (by close order), 20-trade windows ---")
        baseline_last100 = baseline_trades[-100:] if len(baseline_trades) >= 100 else baseline_trades
        baseline_windows = compound_windowed(baseline_last100, 20, STARTING_BALANCE, RISK_PCT, fixed_pct)

        print(f"    {'Variant':>30} | " + " | ".join(f"W{i+1:>6}" for i in range(len(baseline_windows))) + " | Wins-vs-BL")
        print(f"    {'BASELINE':>30} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in baseline_windows) + " |     N/A")

        for label, trades in results.items():
            last100 = trades[-100:] if len(trades) >= 100 else trades
            windows = compound_windowed(last100, 20, STARTING_BALANCE, RISK_PCT, fixed_pct)
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
