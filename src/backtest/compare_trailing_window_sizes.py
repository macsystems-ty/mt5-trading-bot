"""
compare_trailing_window_sizes.py

Tests a range of TRAILING_WINDOW values against our current baseline
(window=3), now that the trend filter (EMA14/1h) and S/R parameters
(swing_lookback=3, retest_tolerance=0.05%) have both already been
updated.

Run with:
    python src/backtest/compare_trailing_window_sizes.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_patterns_combined as combined  # noqa: E402

TRAILING_WINDOW_VALUES_TO_TEST = [1, 2, 3, 4, 5, 7, 10, 15]


def run_variant(candles_5min, trend_series, trailing_window):
    original_window = combined.TRAILING_WINDOW
    combined.TRAILING_WINDOW = trailing_window

    try:
        trades = combined.simulate_combined(candles_5min, trend_series)
        decided = [t for t in trades if t.pct_change is not None]
        if not decided:
            return None
        trades_c = combined.apply_commission(decided, combined.COMMISSION_RATE_PCT)
        wins = [t for t in trades_c if t.pct_change > 0]
        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100

        win_holds = [t.candles_held for t in trades_c if t.pct_change > 0 and t.candles_held is not None]
        loss_holds = [t.candles_held for t in trades_c if t.pct_change <= 0 and t.candles_held is not None]
        avg_win_hold = sum(win_holds) / len(win_holds) if win_holds else 0
        avg_loss_hold = sum(loss_holds) / len(loss_holds) if loss_holds else 0

        return len(trades_c), win_rate, total_return, avg_win_hold, avg_loss_hold
    finally:
        combined.TRAILING_WINDOW = original_window


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)
    print(
        f"Current settings held fixed: EMA14/1h trend, "
        f"SWING_LOOKBACK={combined.SWING_LOOKBACK}, "
        f"RETEST_TOLERANCE_PCT={combined.RETEST_TOLERANCE_PCT}%\n"
    )

    print(
        f"{'Window':>8} | {'Trades':>7} | {'Win Rate':>9} | {'Return':>10} | "
        f"{'Avg Win Hold':>13} | {'Avg Loss Hold':>14}"
    )
    print("-" * 75)

    results = []

    for window in TRAILING_WINDOW_VALUES_TO_TEST:
        result = run_variant(candles_5min, trend_series, window)
        if result is None:
            print(f"{window:>8} | (no trades)")
            continue
        trades, win_rate, total_return, avg_win_hold, avg_loss_hold = result
        results.append((window, trades, win_rate, total_return))
        marker = " <- current" if window == 3 else ""
        print(
            f"{window:>8} | {trades:>7} | {win_rate:>8.1f}% | {total_return:>+9.3f}% | "
            f"{avg_win_hold:>13.1f} | {avg_loss_hold:>14.1f}{marker}"
        )

    if results:
        best = max(results, key=lambda r: r[3])
        print(f"\nBest trailing window by return: {best[0]} ({best[3]:+.3f}%, {best[1]} trades, {best[2]:.1f}% win rate)")

        best_win_rate = max(results, key=lambda r: r[2])
        print(f"Best trailing window by win rate: {best_win_rate[0]} ({best_win_rate[2]:.1f}% win rate, {best_win_rate[3]:+.3f}% return)")


if __name__ == "__main__":
    main()
