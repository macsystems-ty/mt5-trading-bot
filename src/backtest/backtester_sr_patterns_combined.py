"""
backtester_sr_patterns_combined.py

Combines 10 candlestick patterns (all individually-tested patterns
EXCEPT Hammer, Inverted Hammer, and Rising Three Methods, which were
excluded per the latest re-test under current settings) as
alternative confirmation signals at a real S/R level retest:
  - Bullish Engulfing, Bearish Engulfing
  - Piercing Line, Dark Cloud Cover
  - Three White Soldiers, Three Black Crows
  - Morning Star, Evening Star
  - Shooting Star, Falling Three Methods

At each S/R retest, if ANY of the patterns valid for that direction
(BUY patterns at support, SELL patterns at resistance) fires, the
trade is taken. Uses our current validated settings: EMA(14)/1h trend
filter, swing_lookback=3, retest_tolerance=0.05%, trailing-stop
exit window=10.

Run with:
    python src/backtest/backtester_sr_patterns_combined.py
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
    is_bullish_engulfing, is_bearish_engulfing,
    is_piercing_line, is_three_black_crows, is_falling_three_methods,
    is_shooting_star, is_morning_star, is_evening_star,
    is_dark_cloud_cover, is_three_white_soldiers,
)

SWING_LOOKBACK = 3  # adopted after testing: +60.974% combined with tolerance=0.05% (vs baseline 5/0.01%'s +42.593%)
RETEST_TOLERANCE_PCT = 0.05
TRAILING_WINDOW = 10  # adopted after testing: peak return +89.551% (vs window=3's +60.974%), driven by letting winners run ~3x longer than losers
LEVEL_AGE_CAP = 200  # adopted after testing: age_cap=500 won on full-history/last-500 return, but age_cap=200 decisively won on the more recent last-100 and last-50 trade windows ($12,253 vs $11,685, and $11,049 vs $10,361 respectively)

COMMISSION_RATE_PCT = 0.02 / 160 * 100

SELECTED_PATTERNS = {
    "bullish_engulfing": {"candles_needed": 2, "direction": "BUY"},
    "bearish_engulfing": {"candles_needed": 2, "direction": "SELL"},
    "piercing_line": {"candles_needed": 2, "direction": "BUY"},
    "three_black_crows": {"candles_needed": 3, "direction": "SELL"},
    "falling_three_methods": {"candles_needed": 5, "direction": "SELL"},
    "shooting_star": {"candles_needed": 1, "direction": "SELL"},
    "morning_star": {"candles_needed": 3, "direction": "BUY"},
    "evening_star": {"candles_needed": 3, "direction": "SELL"},
    "dark_cloud_cover": {"candles_needed": 2, "direction": "SELL"},
    "three_white_soldiers": {"candles_needed": 3, "direction": "BUY"},
}

MAX_CANDLES_NEEDED = max(p["candles_needed"] for p in SELECTED_PATTERNS.values())

BUY_PATTERNS = [name for name, p in SELECTED_PATTERNS.items() if p["direction"] == "BUY"]
SELL_PATTERNS = [name for name, p in SELECTED_PATTERNS.items() if p["direction"] == "SELL"]


def detect_pattern(pattern_name: str, candles: List[Candle]) -> bool:
    if pattern_name == "bullish_engulfing":
        return is_bullish_engulfing(candles[-2], candles[-1])
    if pattern_name == "bearish_engulfing":
        return is_bearish_engulfing(candles[-2], candles[-1])
    if pattern_name == "piercing_line":
        return is_piercing_line(candles[-2], candles[-1])
    if pattern_name == "three_black_crows":
        return is_three_black_crows(candles[-3], candles[-2], candles[-1])
    if pattern_name == "falling_three_methods":
        return is_falling_three_methods(candles[-5:])
    if pattern_name == "shooting_star":
        return is_shooting_star(candles[-1])
    if pattern_name == "morning_star":
        return is_morning_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "evening_star":
        return is_evening_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "dark_cloud_cover":
        return is_dark_cloud_cover(candles[-2], candles[-1])
    if pattern_name == "three_white_soldiers":
        return is_three_white_soldiers(candles[-3], candles[-2], candles[-1])
    raise ValueError(f"Unknown pattern: {pattern_name}")


def any_pattern_matches(direction: str, candles: List[Candle]) -> Optional[str]:
    candidate_patterns = BUY_PATTERNS if direction == "BUY" else SELL_PATTERNS
    for pattern_name in candidate_patterns:
        needed = SELECTED_PATTERNS[pattern_name]["candles_needed"]
        if detect_pattern(pattern_name, candles[-needed:]):
            return pattern_name
    return None


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    level_price: float
    matched_pattern: str
    initial_stop_distance_pct: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None


def simulate_combined(df: pd.DataFrame, trend_series: pd.Series) -> List[Trade]:
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
    loop_start = max(SWING_LOOKBACK * 2 + 2, MAX_CANDLES_NEEDED + 1)

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
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= LEVEL_AGE_CAP]

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

        if trend not in ("UP", "DOWN"):
            continue

        direction = "BUY" if trend == "UP" else "SELL"
        required_level_type = "support" if direction == "BUY" else "resistance"

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
                for j in range(i - MAX_CANDLES_NEEDED + 1, i + 1)
            ]

            matched_pattern = any_pattern_matches(direction, recent_candles)
            if matched_pattern is None:
                continue

            fill_price = sre.entry_fill_price(current_close, direction)

            window_start = max(0, i - TRAILING_WINDOW + 1)
            initial_stop = (
                min(lows[window_start : i + 1]) if direction == "BUY"
                else max(highs[window_start : i + 1])
            )
            stop_distance_pct = abs(fill_price - initial_stop) / fill_price * 100

            open_trade = Trade(
                direction=direction,
                entry_time=current_time,
                entry_price=fill_price,
                level_price=level_price,
                matched_pattern=matched_pattern,
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
    timeframe = os.getenv("COMBINED_PATTERNS_TIMEFRAME", "5min")
    print(f"Loading {timeframe} candles for {bt.SYMBOL} ...")
    candles = bt.load_candles(timeframe)
    print(f"  Candles: {len(candles):,} (range: {candles.index[0]} to {candles.index[-1]})\n")

    trend_series = build_trend_series_for_range(candles)

    print(f"Combined patterns: {list(SELECTED_PATTERNS.keys())}\n")

    trades = simulate_combined(candles, trend_series)
    decided = [t for t in trades if t.pct_change is not None]
    trades_c = apply_commission(decided, COMMISSION_RATE_PCT)

    wins = [t for t in trades_c if t.pct_change > 0]
    total_return = sum(t.pct_change for t in trades_c)
    win_rate = len(wins) / len(trades_c) * 100 if trades_c else 0

    print("=" * 70)
    print("COMBINED RESULT (10 patterns, real spread + commission)")
    print("=" * 70)
    print(f"Total trades: {len(trades_c)}")
    print(f"Win rate:     {win_rate:.1f}%")
    print(f"Total return: {total_return:+.3f}%")
    print("=" * 70)

    print("\nBreakdown by which pattern triggered each trade:")
    pattern_counts = {}
    pattern_returns = {}
    for t in trades_c:
        pattern_counts[t.matched_pattern] = pattern_counts.get(t.matched_pattern, 0) + 1
        pattern_returns[t.matched_pattern] = pattern_returns.get(t.matched_pattern, 0) + t.pct_change

    for pattern_name in SELECTED_PATTERNS:
        count = pattern_counts.get(pattern_name, 0)
        ret = pattern_returns.get(pattern_name, 0)
        print(f"  {pattern_name:<24}: {count:>5} trades, {ret:+.3f}% contributed")

    print(
        "\nNote: trades may OVERLAP in time between patterns when run\n"
        "separately, so this combined run (which only allows ONE open\n"
        "position at a time) is the more realistic, correct test --\n"
        "do not expect this to simply equal the sum of individual returns."
    )


if __name__ == "__main__":
    main()