"""
diagnose_n2_window2_outcomes.py

Detailed diagnostic for our strongest exit candidate (N=2 favorable
candles before breakeven, Stage 3 trailing window=2): classifies
every trade by what actually happened, and for losing trades
specifically, tracks how far price moved in our favor before the
loss occurred.

Classifications:
  - PURE_LOSS: never reached breakeven at all -- the original stop
    was hit first.
  - PROFIT_WITHOUT_BREAKEVEN: rare edge case, profitable exit without
    ever reaching the breakeven stage.
  - BREAKEVEN_OR_NEAR_ZERO: reached breakeven, exit was a scratch.
  - REAL_PROFIT: reached breakeven, then grew further.
  - REAL_LOSS_AFTER_BREAKEVEN: reached breakeven but still closed as
    a real loss (should be rare/none, given breakeven protection).

Run with:
    python src/backtest/diagnose_n2_window2_outcomes.py
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
NEAR_ZERO_THRESHOLD_PCT = 0.02


@dataclass
class DiagnosticTrade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    matched_pattern: str
    initial_stop_distance_pct: Optional[float] = None
    reached_breakeven: bool = False
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


def simulate_with_diagnostics(df: pd.DataFrame, trend_series: pd.Series) -> List[DiagnosticTrade]:
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

            open_trade = DiagnosticTrade(
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


def classify_outcome(trade: DiagnosticTrade) -> str:
    if not trade.reached_breakeven:
        return "PURE_LOSS" if trade.pct_change <= 0 else "PROFIT_WITHOUT_BREAKEVEN"

    if trade.pct_change > NEAR_ZERO_THRESHOLD_PCT:
        return "REAL_PROFIT"
    if trade.pct_change < -NEAR_ZERO_THRESHOLD_PCT:
        return "REAL_LOSS_AFTER_BREAKEVEN"
    return "BREAKEVEN_OR_NEAR_ZERO"


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print("Running diagnostic simulation (N=2 breakeven, Stage3 window=2) ...\n")
    trades = simulate_with_diagnostics(candles_5min, trend_series)
    decided = [t for t in trades if t.pct_change is not None]

    print(f"Total trades: {len(decided):,}\n")

    print("=" * 70)
    print("OUTCOME CLASSIFICATION")
    print("=" * 70)

    categories = {}
    for t in decided:
        cat = classify_outcome(t)
        categories.setdefault(cat, []).append(t)

    category_labels = {
        "PURE_LOSS": "Pure loss (never reached breakeven, original stop hit)",
        "PROFIT_WITHOUT_BREAKEVEN": "Profit without ever reaching breakeven stage (rare edge case)",
        "BREAKEVEN_OR_NEAR_ZERO": f"Breakeven / near-zero (within +/-{NEAR_ZERO_THRESHOLD_PCT}% of entry)",
        "REAL_PROFIT": "Real profit (reached breakeven, then grew further)",
        "REAL_LOSS_AFTER_BREAKEVEN": "Real loss despite reaching breakeven (should be rare/none)",
    }

    for cat_key, label in category_labels.items():
        cat_trades = categories.get(cat_key, [])
        pct_of_total = len(cat_trades) / len(decided) * 100 if decided else 0
        avg_pct = sum(t.pct_change for t in cat_trades) / len(cat_trades) if cat_trades else 0
        avg_candles_held = sum(t.candles_held for t in cat_trades) / len(cat_trades) if cat_trades else 0
        print(
            f"\n{label}:\n"
            f"  Count: {len(cat_trades):,} ({pct_of_total:.1f}% of all trades)\n"
            f"  Avg pct_change: {avg_pct:+.4f}%\n"
            f"  Avg candles held: {avg_candles_held:.1f}"
        )

    print("\n" + "=" * 70)
    print("FOR PURE LOSSES: how far did price move in our favor (if at all)")
    print("before the original stop was hit?")
    print("=" * 70)

    pure_losses = categories.get("PURE_LOSS", [])
    if pure_losses:
        favorable_excursions = []
        for t in pure_losses:
            if t.max_favorable_price is None:
                favorable_excursions.append(0.0)
                continue
            if t.direction == "BUY":
                max_fav_pct = max(0.0, (t.max_favorable_price - t.entry_price) / t.entry_price * 100)
            else:
                max_fav_pct = max(0.0, (t.entry_price - t.max_favorable_price) / t.entry_price * 100)
            favorable_excursions.append(max_fav_pct)

        zero_progress = sum(1 for f in favorable_excursions if f == 0.0)
        some_progress = [f for f in favorable_excursions if f > 0.0]

        print(f"Pure losses with ZERO favorable movement at all: {zero_progress:,} "
              f"({zero_progress/len(pure_losses)*100:.1f}% of pure losses)")
        if some_progress:
            print(f"Pure losses with SOME favorable movement (but not enough for breakeven):")
            print(f"  Count: {len(some_progress):,} ({len(some_progress)/len(pure_losses)*100:.1f}% of pure losses)")
            print(f"  Avg favorable excursion reached: {sum(some_progress)/len(some_progress):.4f}%")
            print(f"  Max favorable excursion reached: {max(some_progress):.4f}%")
    else:
        print("No pure losses found.")

    total_wins = sum(1 for t in decided if t.pct_change > 0)
    total_return = sum(t.pct_change for t in decided)
    print(f"\nOverall: {total_wins:,}/{len(decided):,} wins ({total_wins/len(decided)*100:.1f}%), "
          f"total return (no commission) {total_return:+.3f}%")


if __name__ == "__main__":
    main()
