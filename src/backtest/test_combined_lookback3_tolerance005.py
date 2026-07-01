"""
test_combined_lookback3_tolerance005.py

Tests the SPECIFIC combination of swing_lookback=3 AND
retest_tolerance=0.05% together (each was previously tested
separately, holding the other at baseline) -- confirming the
combined effect before adopting both as the new baseline.

Run with:
    python src/backtest/test_combined_lookback3_tolerance005.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_patterns_combined as combined  # noqa: E402
import compare_swing_and_retest_params as csr  # noqa: E402


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)
    print("Trend filter: EMA14/1h\n")

    print("Testing COMBINED: swing_lookback=3 AND retest_tolerance=0.05%\n")

    result = csr.run_variant(candles_5min, trend_series, swing_lookback=3, retest_tolerance_pct=0.05)

    if result is None:
        print("No trades generated with this combination.")
        return

    trades, win_rate, total_return = result

    print("=" * 60)
    print("COMBINED RESULT (lookback=3, tolerance=0.05%)")
    print("=" * 60)
    print(f"Trades:     {trades}")
    print(f"Win rate:   {win_rate:.1f}%")
    print(f"Return:     {total_return:+.3f}%")
    print("=" * 60)

    print(
        "\nFor reference, each tested SEPARATELY (holding the other at baseline):\n"
        "  lookback=3 alone:        2,358 trades, 39.4% win rate, +53.948%\n"
        "  tolerance=0.05% alone:   4,059 trades, 38.5% win rate, +64.856%\n"
        "  baseline (5, 0.01%):     1,593 trades, 41.1% win rate, +42.593%\n"
        "\nIf the combined result is notably different from what you'd expect\n"
        "by 'averaging' these two separate effects, that's a sign of a real\n"
        "interaction between the two parameters -- worth knowing before\n"
        "adopting both at once."
    )


if __name__ == "__main__":
    main()
