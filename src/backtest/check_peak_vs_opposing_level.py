"""
check_peak_vs_opposing_level.py

For every WINNING trade in our validated strategy (N=2 breakeven,
Stage3 window=2), finds the nearest OPPOSING-direction S/R level
(resistance for BUY, support for SELL) that existed at entry time,
and checks how close the trade's actual peak favorable price came to
that level before retracing.

This directly tests the hypothesis: "price tends to retrace near the
next opposing S/R level."

Run with:
    python src/backtest/check_peak_vs_opposing_level.py
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

NUM_FAVORABLE_CANDLES_REQUIRED = 2
STAGE3_WINDOW = 2


@dataclass
class DiagnosticTrade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    matched_pattern: str
    nearest_opposing_level_price: Optional[float] = None
    max_favorable_price: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None
    exit_reason: Optional[str] = None


def is_favorable_candle(open_price: float, close_price: float, direction: str) -> bool:
    if direction == "BUY":
        return close_price > open_price
    return close_price < open_price


def simulate_with_opposing_level_tracking(df: pd.DataFrame, trend_series: pd.Series) -> List[DiagnosticTrade]:
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

    trades: List[DiagnosticTrade] = []
    open_trade: Optional[DiagnosticTrade] = None
    open_trade_index: Optional[int] = None
    stop_loss_price: Optional[float] = None
    favorable_candle_count: int = 0
    stage3_active: bool = False

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open = opens[i]
        current_high = highs[i]
        current_low = lows[i]
        current_close = closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= combined.LEVEL_AGE_CAP]

        if open_trade is not None:
            candles_held = i - open_trade_index
            should_close = False
            exit_level = None
            exit_reason = None

            if open_trade.direction == "BUY":
                if open_trade.max_favorable_price is None or current_high > open_trade.max_favorable_price:
                    open_trade.max_favorable_price = current_high
            else:
                if open_trade.max_favorable_price is None or current_low < open_trade.max_favorable_price:
                    open_trade.max_favorable_price = current_low

            if stage3_active:
                window_start = max(0, i - STAGE3_WINDOW)
                if open_trade.direction == "BUY":
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
                    exit_reason = "stage3_trailing_stop_hit"
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
                    if favorable_candle_count >= NUM_FAVORABLE_CANDLES_REQUIRED:
                        stop_loss_price = open_trade.entry_price
                        stage3_active = True
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
                stage3_active = False

            continue

        if trend not in ("UP", "DOWN"):
            continue

        direction = "BUY" if trend == "UP" else "SELL"
        required_level_type = "support" if direction == "BUY" else "resistance"
        opposing_level_type = "resistance" if direction == "BUY" else "support"

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

            opposing_candidates = [
                lvl["price"] for lvl in active_levels
                if lvl["type"] == opposing_level_type
                and (
                    (direction == "BUY" and lvl["price"] > fill_price)
                    or (direction == "SELL" and lvl["price"] < fill_price)
                )
            ]
            nearest_opposing = (
                min(opposing_candidates) if direction == "BUY" and opposing_candidates
                else max(opposing_candidates) if direction == "SELL" and opposing_candidates
                else None
            )

            open_trade = DiagnosticTrade(
                direction=direction,
                entry_time=current_time,
                entry_price=fill_price,
                matched_pattern=matched_pattern,
                nearest_opposing_level_price=nearest_opposing,
            )
            open_trade_index = i
            stop_loss_price = initial_stop
            favorable_candle_count = 0
            stage3_active = False
            break

    return trades


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print("Running simulation with opposing-level tracking ...\n")
    trades = simulate_with_opposing_level_tracking(candles_5min, trend_series)
    decided = [t for t in trades if t.pct_change is not None]

    wins = [t for t in decided if t.pct_change > 0]
    print(f"Total trades: {len(decided):,}")
    print(f"Winning trades: {len(wins):,}\n")

    trades_with_opposing_level = [t for t in wins if t.nearest_opposing_level_price is not None]
    print(
        f"Winning trades that had a known opposing level at entry: "
        f"{len(trades_with_opposing_level):,} ({len(trades_with_opposing_level)/len(wins)*100:.1f}% of wins)\n"
    )

    if not trades_with_opposing_level:
        print("No trades with a known opposing level -- cannot test the hypothesis.")
        return

    print("=" * 70)
    print("HOW CLOSE DID THE PEAK PRICE COME TO THE NEAREST OPPOSING LEVEL?")
    print("(negative = peak fell short of the level, positive = peak overshot it)")
    print("=" * 70)

    distances = []
    for t in trades_with_opposing_level:
        if t.direction == "BUY":
            entry_to_level = t.nearest_opposing_level_price - t.entry_price
            peak_to_level = t.nearest_opposing_level_price - t.max_favorable_price
        else:
            entry_to_level = t.entry_price - t.nearest_opposing_level_price
            peak_to_level = t.max_favorable_price - t.nearest_opposing_level_price

        if entry_to_level <= 0:
            continue

        pct_of_distance_covered = (1 - peak_to_level / entry_to_level) * 100
        distances.append(pct_of_distance_covered)

    if distances:
        within_10pct_of_level = sum(1 for d in distances if 90 <= d <= 110)
        overshot = sum(1 for d in distances if d > 110)
        fell_short = sum(1 for d in distances if d < 90)

        avg_pct_covered = sum(distances) / len(distances)

        print(f"\nAverage % of entry-to-level distance covered by the peak: {avg_pct_covered:.1f}%")
        print(f"  (100% would mean the peak landed EXACTLY at the opposing level)\n")
        print(f"Peak landed within 10% of the level (90-110% of distance): {within_10pct_of_level:,} ({within_10pct_of_level/len(distances)*100:.1f}%)")
        print(f"Peak overshot PAST the level (>110% of distance): {overshot:,} ({overshot/len(distances)*100:.1f}%)")
        print(f"Peak fell SHORT of the level (<90% of distance): {fell_short:,} ({fell_short/len(distances)*100:.1f}%)")

    print("\n" + "=" * 70)
    print("HOW MUCH PROFIT IS LEFT ON THE TABLE? (peak vs actual exit)")
    print("=" * 70)

    give_backs = []
    for t in trades_with_opposing_level:
        if t.direction == "BUY":
            peak_pct = (t.max_favorable_price - t.entry_price) / t.entry_price * 100
        else:
            peak_pct = (t.entry_price - t.max_favorable_price) / t.entry_price * 100
        give_back = peak_pct - t.pct_change
        give_backs.append(give_back)

    avg_give_back = sum(give_backs) / len(give_backs)
    print(f"Average peak-to-exit give-back across these winning trades: {avg_give_back:.4f} percentage points")


if __name__ == "__main__":
    main()
