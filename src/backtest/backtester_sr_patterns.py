"""
backtester_sr_patterns.py

Tests each of the 12 directional candlestick patterns as the
CONFIRMATION signal at a real S/R level retest -- i.e. takes our
VALIDATED entry structure (swing-level detection, retest tolerance,
EMA(50)/1h trend filter, proven trailing-stop exit) and swaps out
ONLY the "is this an Engulfing candle?" check for each pattern in
turn.

Exit: the SAME proven trailing stop (window=3) from our best
validated result (+14.429% over 1 year on 5min candles).

Tests each pattern SEPARATELY first; combining patterns is a planned
follow-up once we see which (if any) perform well individually.

Run with:
    python src/backtest/backtester_sr_patterns.py
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_engulfing as sre  # noqa: E402
from candlestick_patterns import (  # noqa: E402
    Candle,
    is_hammer, is_inverted_hammer, is_shooting_star,
    is_morning_star, is_evening_star,
    is_piercing_line, is_dark_cloud_cover,
    is_three_white_soldiers, is_three_black_crows,
    is_rising_three_methods, is_falling_three_methods,
    is_bullish_engulfing, is_bearish_engulfing,
)

SWING_LOOKBACK = 3  # synced with our current validated setting
RETEST_TOLERANCE_PCT = 0.05
TRAILING_WINDOW = 10

COMMISSION_RATE_PCT = 0.02 / 160 * 100

PATTERN_DEFS = {
    "bullish_engulfing": {"candles_needed": 2, "direction": "BUY"},
    "bearish_engulfing": {"candles_needed": 2, "direction": "SELL"},
    "hammer": {"candles_needed": 1, "direction": "BUY"},
    "inverted_hammer": {"candles_needed": 1, "direction": "BUY"},
    "shooting_star": {"candles_needed": 1, "direction": "SELL"},
    "morning_star": {"candles_needed": 3, "direction": "BUY"},
    "evening_star": {"candles_needed": 3, "direction": "SELL"},
    "piercing_line": {"candles_needed": 2, "direction": "BUY"},
    "dark_cloud_cover": {"candles_needed": 2, "direction": "SELL"},
    "three_white_soldiers": {"candles_needed": 3, "direction": "BUY"},
    "three_black_crows": {"candles_needed": 3, "direction": "SELL"},
    "rising_three_methods": {"candles_needed": 5, "direction": "BUY"},
    "falling_three_methods": {"candles_needed": 5, "direction": "SELL"},
}


def detect_pattern(pattern_name: str, candles: List[Candle]) -> bool:
    if pattern_name == "bullish_engulfing":
        return is_bullish_engulfing(candles[-2], candles[-1])
    if pattern_name == "bearish_engulfing":
        return is_bearish_engulfing(candles[-2], candles[-1])
    if pattern_name == "hammer":
        return is_hammer(candles[-1])
    if pattern_name == "inverted_hammer":
        return is_inverted_hammer(candles[-1])
    if pattern_name == "shooting_star":
        return is_shooting_star(candles[-1])
    if pattern_name == "morning_star":
        return is_morning_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "evening_star":
        return is_evening_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "piercing_line":
        return is_piercing_line(candles[-2], candles[-1])
    if pattern_name == "dark_cloud_cover":
        return is_dark_cloud_cover(candles[-2], candles[-1])
    if pattern_name == "three_white_soldiers":
        return is_three_white_soldiers(candles[-3], candles[-2], candles[-1])
    if pattern_name == "three_black_crows":
        return is_three_black_crows(candles[-3], candles[-2], candles[-1])
    if pattern_name == "rising_three_methods":
        return is_rising_three_methods(candles[-5:])
    if pattern_name == "falling_three_methods":
        return is_falling_three_methods(candles[-5:])
    raise ValueError(f"Unknown pattern: {pattern_name}")


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    level_price: float
    initial_stop_distance_pct: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None


def simulate_pattern_at_sr(df: pd.DataFrame, trend_series: pd.Series, pattern_name: str) -> List[Trade]:
    pattern_def = PATTERN_DEFS[pattern_name]
    needed = pattern_def["candles_needed"]
    pattern_direction = pattern_def["direction"]
    required_trend = "UP" if pattern_direction == "BUY" else "DOWN"
    required_level_type = "support" if pattern_direction == "BUY" else "resistance"

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")

    all_levels = sre.identify_swing_levels(df, SWING_LOOKBACK)
    level_becomes_known_at = {
        lvl["index"] + SWING_LOOKBACK: lvl for lvl in all_levels
    }
    loop_start = max(SWING_LOOKBACK * 2 + 2, needed + 1)

    active_levels: List[dict] = [
        dict(lvl, tested_count=0)
        for known_at, lvl in level_becomes_known_at.items()
        if known_at < loop_start
    ]

    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    open_trade_index: Optional[int] = None
    trailing_stop: Optional[float] = None

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_high = highs[i]
        current_low = lows[i]
        current_close = closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= 500]

        if open_trade is not None:
            candles_held = i - open_trade_index
            window_start = max(0, i - TRAILING_WINDOW)
            if open_trade.direction == "BUY":
                new_stop = min(lows[window_start:i])
                if trailing_stop is None or new_stop > trailing_stop:
                    trailing_stop = new_stop
                should_close = current_low <= trailing_stop
            else:
                new_stop = max(highs[window_start:i])
                if trailing_stop is None or new_stop < trailing_stop:
                    trailing_stop = new_stop
                should_close = current_high >= trailing_stop

            if should_close:
                exit_price = sre.exit_fill_price(trailing_stop, open_trade.direction)
                entry_price = open_trade.entry_price
                if open_trade.direction == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100

                open_trade.exit_time = current_time
                open_trade.exit_price = exit_price
                open_trade.pct_change = pct_change
                open_trade.candles_held = candles_held
                trades.append(open_trade)
                open_trade = None
                open_trade_index = None
                trailing_stop = None

            continue

        if trend != required_trend:
            continue

        for level in active_levels:
            if level["type"] != required_level_type:
                continue

            level_price = level["price"]
            tolerance = level_price * (RETEST_TOLERANCE_PCT / 100)

            if level["type"] == "support":
                retest_price = lows[i - 1]
            else:
                retest_price = highs[i - 1]

            price_near_level = level_price - tolerance <= retest_price <= level_price + tolerance
            if not price_near_level:
                continue

            level["tested_count"] += 1

            recent_candles = [
                Candle(open=opens[j], high=highs[j], low=lows[j], close=closes[j])
                for j in range(i - needed + 1, i + 1)
            ]

            if not detect_pattern(pattern_name, recent_candles):
                continue

            fill_price = sre.entry_fill_price(current_close, pattern_direction)

            window_start = max(0, i - TRAILING_WINDOW + 1)
            initial_stop = (
                min(lows[window_start : i + 1]) if pattern_direction == "BUY"
                else max(highs[window_start : i + 1])
            )
            stop_distance_pct = abs(fill_price - initial_stop) / fill_price * 100

            open_trade = Trade(
                direction=pattern_direction,
                entry_time=current_time,
                entry_price=fill_price,
                level_price=level_price,
                initial_stop_distance_pct=stop_distance_pct,
            )
            open_trade_index = i
            trailing_stop = initial_stop
            break

    return trades


def apply_commission(trades: List[Trade], commission_rate_pct: float) -> List[Trade]:
    adjusted = []
    for t in trades:
        if t.pct_change is None:
            continue
        new_t = Trade(**{**t.__dict__, "pct_change": t.pct_change - commission_rate_pct})
        adjusted.append(new_t)
    return adjusted


def build_trend_series_for_range(entry_candles: pd.DataFrame) -> pd.Series:
    candles_1h = (
        entry_candles.resample("1h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    return bt.compute_ema_1h_trend(candles_1h)


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} (our best validated timeframe) ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,} (range: {candles_5min.index[0]} to {candles_5min.index[-1]})\n")

    trend_series = build_trend_series_for_range(candles_5min)

    print(
        "Testing each candlestick pattern as the S/R-retest confirmation signal\n"
        "(replacing Engulfing), with our current trailing-stop exit (window=10).\n"
    )
    print(f"{'Pattern':<24} | {'Trades':>7} | {'Win Rate':>9} | {'Return':>10}")
    print("-" * 60)

    results = []
    for pattern_name in PATTERN_DEFS:
        trades = simulate_pattern_at_sr(candles_5min, trend_series, pattern_name)
        decided = [t for t in trades if t.pct_change is not None]

        if not decided:
            print(f"{pattern_name:<24} | {'(no trades)':>7} |")
            continue

        trades_c = apply_commission(decided, COMMISSION_RATE_PCT)
        wins = [t for t in trades_c if t.pct_change > 0]
        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100

        results.append((pattern_name, len(trades_c), win_rate, total_return))
        print(f"{pattern_name:<24} | {len(trades_c):>7} | {win_rate:>8.1f}% | {total_return:>+9.3f}%")

    print(
        "\nNote: this re-test uses our CURRENT validated settings (EMA14/1h trend,\n"
        "swing_lookback=3, retest_tolerance=0.05%, trailing_window=10) -- NOT\n"
        "the original settings these patterns were first tested under. Numbers\n"
        "here are not directly comparable to the original individual-pattern\n"
        "test from earlier in this project."
    )

    if results:
        best = max(results, key=lambda r: r[3])
        print(f"\nBest pattern by return: {best[0]} ({best[3]:+.3f}%, {best[1]} trades, {best[2]:.1f}% win rate)")


if __name__ == "__main__":
    main()