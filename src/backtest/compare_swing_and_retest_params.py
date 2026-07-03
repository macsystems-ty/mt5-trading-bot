"""
compare_swing_and_retest_params.py

Tests a range of values for SWING_LOOKBACK and RETEST_TOLERANCE_PCT
against our current baseline (lookback=5, tolerance=0.01%), with our
newly-adopted EMA14/1h trend filter and everything else in the
combined 5-pattern strategy held exactly the same.

Temporarily overrides the module-level constants in
backtester_sr_patterns_combined for each test, then restores the
originals -- this avoids editing the validated baseline file while
still reusing its exact, tested simulate_combined() logic.

Run with:
    python src/backtest/compare_swing_and_retest_params.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_patterns_combined as combined  # noqa: E402

SWING_LOOKBACK_VALUES_TO_TEST = [3, 5, 8, 10, 15]
RETEST_TOLERANCE_VALUES_TO_TEST = [0.005, 0.01, 0.02, 0.03, 0.05]


def run_variant(candles_5min, trend_series, swing_lookback, retest_tolerance_pct):
    original_lookback = combined.SWING_LOOKBACK
    original_tolerance = combined.RETEST_TOLERANCE_PCT

    combined.SWING_LOOKBACK = swing_lookback
    combined.RETEST_TOLERANCE_PCT = retest_tolerance_pct

    try:
        trades = combined.simulate_combined(candles_5min, trend_series)
        decided = [t for t in trades if t.pct_change is not None]
        if not decided:
            return None
        trades_c = combined.apply_commission(decided, combined.COMMISSION_RATE_PCT)
        wins = [t for t in trades_c if t.pct_change > 0]
        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100
        return len(trades_c), win_rate, total_return
    finally:
        combined.SWING_LOOKBACK = original_lookback
        combined.RETEST_TOLERANCE_PCT = original_tolerance


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)
    print("Trend filter: EMA14/1h (our newly-adopted baseline)\n")

    print("=" * 70)
    print("PART 1: Testing SWING_LOOKBACK (retest tolerance held at baseline 0.01%)")
    print("=" * 70)
    print(f"{'Lookback':>10} | {'Trades':>7} | {'Win Rate':>9} | {'Return':>10}")
    print("-" * 50)

    lookback_results = []
    for lookback in SWING_LOOKBACK_VALUES_TO_TEST:
        result = run_variant(candles_5min, trend_series, lookback, combined.RETEST_TOLERANCE_PCT)
        if result is None:
            print(f"{lookback:>10} | (no trades)")
            continue
        trades, win_rate, total_return = result
        lookback_results.append((lookback, trades, win_rate, total_return))
        marker = " <- baseline" if lookback == 5 else ""
        print(f"{lookback:>10} | {trades:>7} | {win_rate:>8.1f}% | {total_return:>+9.3f}%{marker}")

    print("\n" + "=" * 70)
    print("PART 2: Testing RETEST_TOLERANCE_PCT (swing lookback held at baseline 5)")
    print("=" * 70)
    print(f"{'Tolerance %':>11} | {'Trades':>7} | {'Win Rate':>9} | {'Return':>10}")
    print("-" * 50)

    tolerance_results = []
    for tolerance in RETEST_TOLERANCE_VALUES_TO_TEST:
        result = run_variant(candles_5min, trend_series, combined.SWING_LOOKBACK, tolerance)
        if result is None:
            print(f"{tolerance:>11} | (no trades)")
            continue
        trades, win_rate, total_return = result
        tolerance_results.append((tolerance, trades, win_rate, total_return))
        marker = " <- baseline" if tolerance == 0.01 else ""
        print(f"{tolerance:>11} | {trades:>7} | {win_rate:>8.1f}% | {total_return:>+9.3f}%{marker}")

    print(
        "\nFor reference, our current baseline (lookback=5, tolerance=0.01%,\n"
        "with the NEW EMA14/1h trend filter) should match: +42.593% return,\n"
        "41.1% win rate, 1593 trades (from compare_trend_filter_speeds.py)."
    )

    if lookback_results:
        best_lookback = max(lookback_results, key=lambda r: r[3])
        print(f"\nBest swing lookback by return: {best_lookback[0]} ({best_lookback[3]:+.3f}%)")
    if tolerance_results:
        best_tolerance = max(tolerance_results, key=lambda r: r[3])
        print(f"Best retest tolerance by return: {best_tolerance[0]}% ({best_tolerance[3]:+.3f}%)")


if __name__ == "__main__":
    main()
