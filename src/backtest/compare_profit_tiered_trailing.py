"""
compare_profit_tiered_trailing.py

Tests a PROFIT-TIERED Stage 3 trailing window: instead of a fixed
window=2 throughout Stage 3, the window shrinks as unrealized profit
grows past certain thresholds -- locking in more of a large gain
before it can retrace.

Each tier structure is a list of (profit_threshold_pct, window) pairs.
At each candle, the position's CURRENT unrealized profit determines
which tier's window is used.

Run with:
    python src/backtest/compare_profit_tiered_trailing.py
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_engulfing as sre  # noqa: E402
import backtester_sr_patterns_combined as combined  # noqa: E402

NUM_FAVORABLE_CANDLES_REQUIRED = 2

TIER_STRUCTURES_TO_TEST = {
    "flat_window2 (current)": [(0.0, 2)],
    "tighten_at_0.2pct": [(0.0, 2), (0.2, 1)],
    "tighten_at_0.3pct": [(0.0, 2), (0.3, 1)],
    "tighten_at_0.4pct": [(0.0, 2), (0.4, 1)],
    "tighten_at_0.5pct": [(0.0, 2), (0.5, 1)],
    "tighten_at_0.7pct": [(0.0, 2), (0.7, 1)],
    "tighten_at_1.0pct": [(0.0, 2), (1.0, 1)],
    "gentle_0.5_0.8": [(0.0, 2), (0.5, 2), (0.8, 1)],
    "gentle_0.4_0.7": [(0.0, 2), (0.4, 2), (0.7, 1)],
    "gentle_0.6_1.0": [(0.0, 2), (0.6, 2), (1.0, 1)],
    "three_tier_0.3_0.6_1.0": [(0.0, 2), (0.3, 1), (0.6, 1), (1.0, 1)],
    "early_tighten_0.1": [(0.0, 2), (0.1, 1)],
}


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


def get_window_for_profit(tiers: List[Tuple[float, int]], current_profit_pct: float) -> int:
    window = tiers[0][1]
    for threshold, w in tiers:
        if current_profit_pct >= threshold:
            window = w
    return window


def simulate(df: pd.DataFrame, trend_series: pd.Series, tiers: List[Tuple[float, int]]) -> List[Trade]:
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

            if stage3_active:
                if open_trade.direction == "BUY":
                    current_profit_pct = (current_close - open_trade.entry_price) / open_trade.entry_price * 100
                else:
                    current_profit_pct = (open_trade.entry_price - current_close) / open_trade.entry_price * 100

                stage3_window = get_window_for_profit(tiers, current_profit_pct)

                window_start = max(0, i - stage3_window)
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
            stage3_active = False
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


def compound(trades, starting_balance=10000.0, risk_pct=1.0):
    balance = starting_balance
    for t in trades:
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print(
        f"{'Tier Structure':<26} | {'Trades':>7} | {'Win Rate':>9} | {'Full Return':>12} | "
        f"{'Last 500 ($10k->)':>18}"
    )
    print("-" * 85)

    for label, tiers in TIER_STRUCTURES_TO_TEST.items():
        trades = simulate(candles_5min, trend_series, tiers)
        decided = [t for t in trades if t.pct_change is not None]
        trades_c = apply_commission(decided, combined.COMMISSION_RATE_PCT)

        if not trades_c:
            print(f"{label:<26} | (no trades)")
            continue

        wins = [t for t in trades_c if t.pct_change > 0]
        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100

        last_500 = trades_c[-500:] if len(trades_c) >= 500 else trades_c
        final_balance = compound(last_500)

        marker = " <- current" if label.startswith("flat_window2") else ""
        print(
            f"{label:<26} | {len(trades_c):>7} | {win_rate:>8.1f}% | {total_return:>+11.3f}% | "
            f"${final_balance:>16,.2f}{marker}"
        )

    print(
        "\nFor reference, our current best (flat window=2, batch-tested):\n"
        "  4,954 trades, 44.5% win rate, full return +139.463%, last 500\n"
        "  trades $10,000 -> $13,211.60 (+33.1% compounded)."
    )


if __name__ == "__main__":
    main()