"""
compare_trend_filter_speeds.py

Tests a range of trend-filter speeds -- different EMA periods AND
different resample timeframes for the trend calculation -- against
our current validated baseline (EMA(50) on 1H candles), while
keeping EVERYTHING else in the combined 5-pattern strategy exactly
the same (S/R retest, candlestick pattern confirmation, trailing
stop exit).

Run with:
    python src/backtest/compare_trend_filter_speeds.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_patterns_combined as combined  # noqa: E402

VARIANTS_TO_TEST = [
    ("BASELINE: EMA50 on 1h", "1h", 50),
    ("EMA30 on 1h", "1h", 30),
    ("EMA20 on 1h", "1h", 20),
    ("EMA14 on 1h", "1h", 14),
    ("EMA50 on 30min", "30min", 50),
    ("EMA20 on 30min", "30min", 20),
    ("EMA50 on 15min", "15min", 50),
    ("EMA20 on 15min", "15min", 20),
]


def compute_custom_ema_trend(candles_5min: pd.DataFrame, resample_tf: str, ema_period: int) -> pd.Series:
    candles_custom = (
        candles_5min.resample(resample_tf)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )

    ema = candles_custom["close"].ewm(span=ema_period, adjust=False).mean()
    ema_rising = ema.diff() > 0
    price_above = candles_custom["close"] > ema

    trend = pd.Series("FLAT", index=candles_custom.index)
    trend[price_above & ema_rising] = "UP"
    trend[(~price_above) & (~ema_rising)] = "DOWN"

    return trend


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    print(
        "Testing trend-filter speeds. Everything else (S/R retest, candlestick\n"
        "patterns, trailing stop exit) stays EXACTLY the same as our validated\n"
        "combined 5-pattern strategy -- only the trend filter's speed changes.\n"
    )

    print(
        f"{'Variant':<24} | {'Trades':>7} | {'Win Rate':>9} | {'Return':>10} | "
        f"{'Trend flips':>12}"
    )
    print("-" * 75)

    results = []

    for label, resample_tf, ema_period in VARIANTS_TO_TEST:
        trend_series = compute_custom_ema_trend(candles_5min, resample_tf, ema_period)

        flips = (trend_series != trend_series.shift(1)).sum()

        trades = combined.simulate_combined(candles_5min, trend_series)
        decided = [t for t in trades if t.pct_change is not None]

        if not decided:
            print(f"{label:<24} | {'(no trades)':>7} |")
            continue

        trades_c = combined.apply_commission(decided, combined.COMMISSION_RATE_PCT)
        wins = [t for t in trades_c if t.pct_change > 0]
        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100

        results.append((label, len(trades_c), win_rate, total_return, flips))

        print(
            f"{label:<24} | {len(trades_c):>7} | {win_rate:>8.1f}% | "
            f"{total_return:>+9.3f}% | {flips:>12,}"
        )

    print(
        "\nFor reference, our validated baseline result on this exact dataset:\n"
        "1,597 trades, 37.9% win rate, +19.936% return.\n"
        "\nA faster trend filter that flips MUCH more often (see 'Trend flips'\n"
        "column) is more likely reacting to noise than to real reversals --\n"
        "this was exactly the failure mode we found and rejected with\n"
        "EMA5/13 on 1min candles earlier in this project."
    )

    if results:
        best = max(results, key=lambda r: r[3])
        print(f"\nBest variant by return: {best[0]} ({best[3]:+.3f}%, {best[1]} trades, {best[2]:.1f}% win rate)")


if __name__ == "__main__":
    main()
