"""
compare_stateful_structure_break.py

Tests a STATEFUL market-structure filter: once a structural break
occurs (a Lower High during an uptrend, or Higher Low during a
downtrend), entries in that direction are blocked entirely until
structure freshly re-confirms with two new consecutive confirming
swings -- distinct from the earlier rolling-window check.

Run with:
    python src/backtest/compare_stateful_structure_break.py
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


class StructureTracker:
    def __init__(self):
        self.last_resistance_price: Optional[float] = None
        self.last_support_price: Optional[float] = None
        self.uptrend_valid: bool = True
        self.downtrend_valid: bool = True
        self._uptrend_seen_hh_since_break: bool = False
        self._uptrend_seen_hl_since_break: bool = False
        self._downtrend_seen_ll_since_break: bool = False
        self._downtrend_seen_lh_since_break: bool = False

    def update_with_new_resistance(self, price: float) -> None:
        if self.last_resistance_price is not None:
            if price > self.last_resistance_price:
                if not self.uptrend_valid:
                    self._uptrend_seen_hh_since_break = True
                    if self._uptrend_seen_hl_since_break:
                        self.uptrend_valid = True
                        self._uptrend_seen_hh_since_break = False
                        self._uptrend_seen_hl_since_break = False
            else:
                self.uptrend_valid = False
                self._uptrend_seen_hh_since_break = False
                self._uptrend_seen_hl_since_break = False

                if not self.downtrend_valid:
                    self._downtrend_seen_lh_since_break = True
                    if self._downtrend_seen_ll_since_break:
                        self.downtrend_valid = True
                        self._downtrend_seen_ll_since_break = False
                        self._downtrend_seen_lh_since_break = False

        self.last_resistance_price = price

    def update_with_new_support(self, price: float) -> None:
        if self.last_support_price is not None:
            if price < self.last_support_price:
                if not self.downtrend_valid:
                    self._downtrend_seen_ll_since_break = True
                    if self._downtrend_seen_lh_since_break:
                        self.downtrend_valid = True
                        self._downtrend_seen_ll_since_break = False
                        self._downtrend_seen_lh_since_break = False
            else:
                self.downtrend_valid = False
                self._downtrend_seen_ll_since_break = False
                self._downtrend_seen_lh_since_break = False

                if not self.uptrend_valid:
                    self._uptrend_seen_hl_since_break = True
                    if self._uptrend_seen_hh_since_break:
                        self.uptrend_valid = True
                        self._uptrend_seen_hh_since_break = False
                        self._uptrend_seen_hl_since_break = False

        self.last_support_price = price


def is_favorable_candle(open_price: float, close_price: float, direction: str) -> bool:
    if direction == "BUY":
        return close_price > open_price
    return close_price < open_price


def simulate(df: pd.DataFrame, trend_series: pd.Series, use_structure_filter: bool) -> List[Trade]:
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

    active_levels: List[dict] = []
    structure = StructureTracker()

    for known_at in sorted(k for k in level_becomes_known_at if k < loop_start):
        lvl = level_becomes_known_at[known_at]
        active_levels.append(dict(lvl, tested_count=0))
        if lvl["type"] == "resistance":
            structure.update_with_new_resistance(lvl["price"])
        else:
            structure.update_with_new_support(lvl["price"])

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
            new_level = level_becomes_known_at[i]
            active_levels.append(dict(new_level, tested_count=0))
            if new_level["type"] == "resistance":
                structure.update_with_new_resistance(new_level["price"])
            else:
                structure.update_with_new_support(new_level["price"])

        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= combined.LEVEL_AGE_CAP]

        if open_trade is not None:
            candles_held = i - open_trade_index
            should_close = False
            exit_level = None
            exit_reason = None

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

        if use_structure_filter:
            if direction == "BUY" and not structure.uptrend_valid:
                continue
            if direction == "SELL" and not structure.downtrend_valid:
                continue

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

    print(f"{'Filter':>20} | {'Trades':>7} | {'Win Rate':>9} | {'Full Return':>12} | {'Last 500 ($10k->)':>18}")
    print("-" * 75)

    for label, use_filter in [("no_filter (current)", False), ("stateful_break_filter", True)]:
        trades = simulate(candles_5min, trend_series, use_filter)
        decided = [t for t in trades if t.pct_change is not None]
        trades_c = apply_commission(decided, combined.COMMISSION_RATE_PCT)

        if not trades_c:
            print(f"{label:>20} | (no trades)")
            continue

        wins = [t for t in trades_c if t.pct_change > 0]
        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100

        last_500 = trades_c[-500:] if len(trades_c) >= 500 else trades_c
        final_balance = compound(last_500)

        print(
            f"{label:>20} | {len(trades_c):>7} | {win_rate:>8.1f}% | {total_return:>+11.3f}% | "
            f"${final_balance:>16,.2f}"
        )

    print(
        "\nFor reference, our current best (no structure filter, batch-tested):\n"
        "  4,954 trades, 44.5% win rate, full return +139.463%, last 500\n"
        "  trades $10,000 -> $13,211.60 (+33.1% compounded)."
    )


if __name__ == "__main__":
    main()
