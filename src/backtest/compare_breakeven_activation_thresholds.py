"""
compare_breakeven_activation_thresholds.py

Tests requiring N consecutive favorable candles (1, 2, or 3) before
the breakeven-then-close-on-counter-candle mechanism activates,
instead of just 1.

Run with:
    python src/backtest/compare_breakeven_activation_thresholds.py
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
import backtester_sr_patterns_combined as combined  # noqa: E402

NUM_FAVORABLE_CANDLES_VALUES_TO_TEST = [1, 2, 3]


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    matched_pattern: str
    initial_stop_distance_pct: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None
    exit_reason: Optional[str] = None


def is_favorable_candle(open_price: float, close_price: float, direction: str) -> bool:
    if direction == "BUY":
        return close_price > open_price
    return close_price < open_price


def simulate_breakeven_then_close(
    df: pd.DataFrame, trend_series: pd.Series, num_favorable_candles_required: int
) -> List[Trade]:
    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")

    all_levels = sre.identify_swing_levels(df, combined.SWING_LOOKBACK)
    level_becomes_known_at = {
        lvl["index"] + combined.SWING_LOOKBACK: lvl for lvl in all_levels
    }
    loop_start = max(combined.SWING_LOOKBACK * 2 + 2, combined.MAX_CANDLES_NEEDED + 1)

    active_levels: List[dict] = [
        dict(lvl, tested_count=0)
        for known_at, lvl in level_becomes_known_at.items()
        if known_at < loop_start
    ]

    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    open_trade_index: Optional[int] = None
    stop_loss_price: Optional[float] = None
    favorable_candle_count: int = 0
    counter_candle_close_active: bool = False

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open = opens[i]
        current_high = highs[i]
        current_low = lows[i]
        current_close = closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= 500]

        if open_trade is not None:
            candles_held = i - open_trade_index
            should_close = False
            exit_level = None
            exit_reason = None

            if counter_candle_close_active:
                favorable = is_favorable_candle(current_open, current_close, open_trade.direction)
                if not favorable:
                    should_close = True
                    exit_level = current_close
                    exit_reason = "counter_candle_close"
            else:
                if open_trade.direction == "BUY":
                    hit_stop = current_low <= stop_loss_price
                else:
                    hit_stop = current_high >= stop_loss_price

                if hit_stop:
                    should_close = True
                    exit_level = stop_loss_price
                    exit_reason = "initial_or_breakeven_stop_hit"

                favorable = is_favorable_candle(current_open, current_close, open_trade.direction)
                if favorable:
                    favorable_candle_count += 1
                    if favorable_candle_count >= num_favorable_candles_required:
                        stop_loss_price = open_trade.entry_price
                        counter_candle_close_active = True
                else:
                    favorable_candle_count = 0

            if should_close:
                exit_price = sre.exit_fill_price(exit_level, open_trade.direction)
                entry_price = open_trade.entry_price

                if open_trade.direction == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100

                open_trade.exit_time = current_time
                open_trade.exit_price = exit_price
                open_trade.pct_change = pct_change
                open_trade.candles_held = candles_held
                open_trade.exit_reason = exit_reason

                trades.append(open_trade)
                open_trade = None
                open_trade_index = None
                stop_loss_price = None
                favorable_candle_count = 0
                counter_candle_close_active = False

            continue

        if trend not in ("UP", "DOWN"):
            continue

        direction = "BUY" if trend == "UP" else "SELL"
        required_level_type = "support" if direction == "BUY" else "resistance"

        for level in active_levels:
            if level["type"] != required_level_type:
                continue

            level_price = level["price"]
            tolerance = level_price * (combined.RETEST_TOLERANCE_PCT / 100)

            if level["type"] == "support":
                retest_price = lows[i - 1]
            else:
                retest_price = highs[i - 1]

            price_near_level = level_price - tolerance <= retest_price <= level_price + tolerance
            if not price_near_level:
                continue

            level["tested_count"] += 1

            recent_candles = [
                combined.Candle(open=opens[j], high=highs[j], low=lows[j], close=closes[j])
                for j in range(i - combined.MAX_CANDLES_NEEDED + 1, i + 1)
            ]

            matched_pattern = combined.any_pattern_matches(direction, recent_candles)
            if matched_pattern is None:
                continue

            fill_price = sre.entry_fill_price(current_close, direction)

            window_start = max(0, i - combined.TRAILING_WINDOW + 1)
            initial_stop = (
                min(lows[window_start : i + 1]) if direction == "BUY"
                else max(highs[window_start : i + 1])
            )
            stop_distance_pct = abs(fill_price - initial_stop) / fill_price * 100

            open_trade = Trade(
                direction=direction,
                entry_time=current_time,
                entry_price=fill_price,
                matched_pattern=matched_pattern,
                initial_stop_distance_pct=stop_distance_pct,
            )
            open_trade_index = i
            stop_loss_price = initial_stop
            favorable_candle_count = 0
            counter_candle_close_active = False
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


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print(
        f"{'Candles req.':>12} | {'Trades':>7} | {'Win Rate':>9} | {'Return (full)':>14} | "
        f"{'Return (last 500)':>18}"
    )
    print("-" * 70)

    for n in NUM_FAVORABLE_CANDLES_VALUES_TO_TEST:
        trades = simulate_breakeven_then_close(candles_5min, trend_series, n)
        decided = [t for t in trades if t.pct_change is not None]
        trades_c = apply_commission(decided, combined.COMMISSION_RATE_PCT)

        wins = [t for t in trades_c if t.pct_change > 0]
        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100 if trades_c else 0

        last_500 = trades_c[-500:] if len(trades_c) >= 500 else trades_c
        last_500_return = sum(t.pct_change for t in last_500)

        print(
            f"{n:>12} | {len(trades_c):>7} | {win_rate:>8.1f}% | {total_return:>+13.3f}% | "
            f"{last_500_return:>+17.3f}%"
        )

    print(
        "\nFor reference:\n"
        "  N=1 (already tested): 7,257 trades, 47.7% win rate, +26.457% full-history return,\n"
        "  but the most recent 500 trades LOST money ($10,000 -> $8,974.54, -10.25%).\n"
        "  Our current trailing-window strategy: 4,068 trades, 35.8% win rate, +104.018%\n"
        "  full-history return, and the most recent 500 trades GAINED +35.2%."
    )


if __name__ == "__main__":
    main()
