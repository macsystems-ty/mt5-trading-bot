"""
show_stoploss_candles.py

For every PURE LOSS trade (never reached breakeven) in our current
best strategy (N=2 breakeven, Stage3 window=2, full stop width),
captures the actual candle OHLC data around entry, breaks losses
down by which pattern triggered them, and prints concrete real
examples for inspection.

Run with:
    python src/backtest/show_stoploss_candles.py
"""

import os
import sys
from dataclasses import dataclass, field
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
MAX_EXAMPLES_PER_PATTERN = 3


@dataclass
class CandleSnapshot:
    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float


@dataclass
class DiagnosticTrade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    matched_pattern: str
    initial_stop_price: float
    initial_stop_distance_pct: Optional[float] = None
    reached_breakeven: bool = False
    entry_setup_candles: List[CandleSnapshot] = field(default_factory=list)
    post_entry_candles: List[CandleSnapshot] = field(default_factory=list)
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None
    exit_reason: Optional[str] = None


def is_favorable_candle(open_price: float, close_price: float, direction: str) -> bool:
    if direction == "BUY":
        return close_price > open_price
    return close_price < open_price


def simulate_with_candle_capture(df: pd.DataFrame, trend_series: pd.Series) -> List[DiagnosticTrade]:
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
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= 500]

        if open_trade is not None:
            candles_held = i - open_trade_index
            should_close = False
            exit_level = None
            exit_reason = None

            open_trade.post_entry_candles.append(
                CandleSnapshot(current_time, current_open, current_high, current_low, current_close)
            )

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
                        open_trade.reached_breakeven = True
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

            needed = combined.SELECTED_PATTERNS[matched_pattern]["candles_needed"]
            entry_setup_candles = [
                CandleSnapshot(times[j], opens[j], highs[j], lows[j], closes[j])
                for j in range(i - needed + 1, i + 1)
            ]

            open_trade = DiagnosticTrade(
                direction=direction,
                entry_time=current_time,
                entry_price=fill_price,
                matched_pattern=matched_pattern,
                initial_stop_price=initial_stop,
                initial_stop_distance_pct=stop_distance_pct,
                entry_setup_candles=entry_setup_candles,
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

    print("Running simulation with candle capture ...\n")
    trades = simulate_with_candle_capture(candles_5min, trend_series)
    decided = [t for t in trades if t.pct_change is not None]

    pure_losses = [t for t in decided if not t.reached_breakeven and t.pct_change <= 0]

    print(f"Total trades: {len(decided):,}")
    print(f"Pure losses (never reached breakeven): {len(pure_losses):,}\n")

    print("=" * 70)
    print("PURE LOSSES BY PATTERN")
    print("=" * 70)

    by_pattern = {}
    for t in pure_losses:
        by_pattern.setdefault(t.matched_pattern, []).append(t)

    print(f"{'Pattern':<24} | {'Count':>7} | {'% of pure losses':>17} | {'Avg loss':>10}")
    print("-" * 65)
    for pattern_name, pattern_trades in sorted(by_pattern.items(), key=lambda kv: -len(kv[1])):
        pct = len(pattern_trades) / len(pure_losses) * 100
        avg_loss = sum(t.pct_change for t in pattern_trades) / len(pattern_trades)
        print(f"{pattern_name:<24} | {len(pattern_trades):>7} | {pct:>16.1f}% | {avg_loss:>+9.4f}%")

    print("\n" + "=" * 70)
    print(f"EXAMPLE CANDLES (up to {MAX_EXAMPLES_PER_PATTERN} per pattern)")
    print("=" * 70)

    for pattern_name, pattern_trades in sorted(by_pattern.items(), key=lambda kv: -len(kv[1])):
        print(f"\n--- {pattern_name} ({len(pattern_trades)} pure losses) ---")
        for t in pattern_trades[:MAX_EXAMPLES_PER_PATTERN]:
            print(f"\n  Trade: {t.direction} @ {t.entry_price:.2f}, entry_time={t.entry_time}")
            print(f"  Initial stop: {t.initial_stop_price:.2f} (distance: {t.initial_stop_distance_pct:.4f}%)")
            print("  Entry setup candles (the ones that triggered the pattern):")
            for c in t.entry_setup_candles:
                color = "GREEN" if c.close > c.open else ("RED" if c.close < c.open else "FLAT")
                print(
                    f"    {c.time} | O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f} [{color}]"
                )
            print("  Candles after entry, leading to the stop being hit:")
            for c in t.post_entry_candles:
                color = "GREEN" if c.close > c.open else ("RED" if c.close < c.open else "FLAT")
                print(
                    f"    {c.time} | O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f} [{color}]"
                )
            print(f"  Exit: {t.exit_price:.2f} ({t.pct_change:+.4f}%), reason={t.exit_reason}")


if __name__ == "__main__":
    main()
