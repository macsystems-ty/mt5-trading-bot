"""
diagnose_losing_trades.py

Re-runs our validated combined-pattern strategy (same entry logic,
same trailing-stop exit), but tracks ADDITIONAL detail per trade
needed to classify exactly what happened during each LOSING trade.

Categories:
  1. IMMEDIATE_ADVERSE: price never moved favorably at all -- the
     trade was a loser from candle 1, the stop never got a chance to
     trail.
  2. REVERSED_AFTER_PROGRESS: price moved favorably for a while (the
     trailing stop DID move from its initial level), but then
     reversed far enough to hit the (now-trailed) stop anyway.
  3. NO_PROGRESS_NO_REVERSAL_NEEDED: some favorable movement occurred
     but never enough to move the trailing stop at all, so the
     original stop was hit without ever trailing.

Run with:
    python src/backtest/diagnose_losing_trades.py
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


@dataclass
class DiagnosticTrade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    matched_pattern: str
    initial_stop_price: float
    max_favorable_price: Optional[float] = None
    trailing_stop_ever_moved: bool = False
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None
    initial_stop_distance_pct: Optional[float] = None


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
            window_start = max(0, i - combined.TRAILING_WINDOW)

            if open_trade.direction == "BUY":
                if open_trade.max_favorable_price is None or current_high > open_trade.max_favorable_price:
                    open_trade.max_favorable_price = current_high

                new_stop = min(lows[window_start:i])
                if trailing_stop is None or new_stop > trailing_stop:
                    if new_stop != open_trade.initial_stop_price:
                        open_trade.trailing_stop_ever_moved = True
                    trailing_stop = new_stop
                should_close = current_low <= trailing_stop
            else:
                if open_trade.max_favorable_price is None or current_low < open_trade.max_favorable_price:
                    open_trade.max_favorable_price = current_low

                new_stop = max(highs[window_start:i])
                if trailing_stop is None or new_stop < trailing_stop:
                    if new_stop != open_trade.initial_stop_price:
                        open_trade.trailing_stop_ever_moved = True
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
                initial_stop_price=initial_stop,
                initial_stop_distance_pct=stop_distance_pct,
            )
            open_trade_index = i
            trailing_stop = initial_stop
            break

    return trades


def classify_loss(trade: DiagnosticTrade) -> str:
    if trade.max_favorable_price is None:
        return "IMMEDIATE_ADVERSE"

    if trade.direction == "BUY":
        showed_favorable_movement = trade.max_favorable_price > trade.entry_price
    else:
        showed_favorable_movement = trade.max_favorable_price < trade.entry_price

    if not showed_favorable_movement:
        return "IMMEDIATE_ADVERSE"

    if trade.trailing_stop_ever_moved:
        return "REVERSED_AFTER_PROGRESS"

    return "NO_PROGRESS_NO_REVERSAL_NEEDED"


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print("Running diagnostic simulation (tracks extra detail per trade) ...\n")
    trades = simulate_with_diagnostics(candles_5min, trend_series)
    decided = [t for t in trades if t.pct_change is not None]

    losses = [t for t in decided if t.pct_change <= 0]
    wins = [t for t in decided if t.pct_change > 0]

    print(f"Total trades: {len(decided):,}")
    print(f"Wins: {len(wins):,} | Losses: {len(losses):,}\n")

    print("=" * 70)
    print("LOSS CLASSIFICATION (before commission, raw price-action breakdown)")
    print("=" * 70)

    categories = {}
    for t in losses:
        cat = classify_loss(t)
        categories.setdefault(cat, []).append(t)

    category_labels = {
        "IMMEDIATE_ADVERSE": "Immediate adverse move (never went favorable at all)",
        "REVERSED_AFTER_PROGRESS": "Showed real progress, then reversed (stop DID trail)",
        "NO_PROGRESS_NO_REVERSAL_NEEDED": "Some favorable movement, but never enough to trail the stop",
    }

    for cat_key, label in category_labels.items():
        cat_trades = categories.get(cat_key, [])
        pct_of_losses = len(cat_trades) / len(losses) * 100 if losses else 0
        avg_loss_pct = sum(t.pct_change for t in cat_trades) / len(cat_trades) if cat_trades else 0
        avg_candles_held = sum(t.candles_held for t in cat_trades) / len(cat_trades) if cat_trades else 0
        print(
            f"\n{label}:\n"
            f"  Count: {len(cat_trades):,} ({pct_of_losses:.1f}% of all losses)\n"
            f"  Avg loss size: {avg_loss_pct:+.4f}%\n"
            f"  Avg candles held: {avg_candles_held:.1f}"
        )

    print("\n" + "=" * 70)
    print("For trades that reversed after showing progress: how much profit")
    print("was given back, on average, before the stop was hit?")
    print("=" * 70)

    reversed_trades = categories.get("REVERSED_AFTER_PROGRESS", [])
    if reversed_trades:
        give_backs = []
        for t in reversed_trades:
            if t.direction == "BUY":
                max_favorable_pct = (t.max_favorable_price - t.entry_price) / t.entry_price * 100
            else:
                max_favorable_pct = (t.entry_price - t.max_favorable_price) / t.entry_price * 100
            give_back = max_favorable_pct - t.pct_change
            give_backs.append(give_back)

        avg_give_back = sum(give_backs) / len(give_backs)
        max_give_back = max(give_backs)
        print(f"Average peak-to-exit give-back: {avg_give_back:.4f} percentage points")
        print(f"Largest single give-back: {max_give_back:.4f} percentage points")
    else:
        print("No trades in this category.")


if __name__ == "__main__":
    main()
