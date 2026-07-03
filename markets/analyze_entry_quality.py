"""
analyze_entry_quality.py

Breaks down REAL trade outcomes by three different angles, using the
existing validated backtest_strategy.simulate() output directly:

  1. BY PATTERN TYPE: which of the 10 candlestick patterns produce
     the best win rate / average return?
  2. BY HOUR OF DAY (UTC): do certain hours produce better trades?
  3. BY TREND STRENGTH AT ENTRY: does a stronger trend (price further
     from the EMA) correlate with better outcomes?

Run this FROM INSIDE your markets/ folder:
    cd markets
    python analyze_entry_quality.py
"""

import importlib.util
import os
import statistics
from collections import defaultdict

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))
MARKETS_TO_CHECK = ["1HZ25V", "1HZ75V", "1HZ90V", "1HZ100V", "R_100"]


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summarize_group(trades):
    if not trades:
        return None
    wins = sum(1 for t in trades if t.pct_change > 0)
    win_rate = wins / len(trades) * 100
    avg_return = statistics.mean(t.pct_change for t in trades)
    return {"count": len(trades), "win_rate": win_rate, "avg_return": avg_return}


def analyze_by_pattern(trades):
    by_pattern = defaultdict(list)
    for t in trades:
        by_pattern[t.matched_pattern].append(t)

    print(f"  {'Pattern':>25} | {'Trades':>7} | {'Win Rate':>9} | {'Avg Return/Trade':>17}")
    print("  " + "-" * 65)
    for pattern, group in sorted(by_pattern.items(), key=lambda kv: -len(kv[1])):
        summary = summarize_group(group)
        print(
            f"  {pattern:>25} | {summary['count']:>7} | {summary['win_rate']:>8.1f}% | "
            f"{summary['avg_return']:>+16.4f}%"
        )


def analyze_by_hour(trades):
    by_hour = defaultdict(list)
    for t in trades:
        hour = t.entry_time.hour
        by_hour[hour].append(t)

    print(f"  {'Hour (UTC)':>10} | {'Trades':>7} | {'Win Rate':>9} | {'Avg Return/Trade':>17}")
    print("  " + "-" * 55)
    for hour in sorted(by_hour.keys()):
        group = by_hour[hour]
        summary = summarize_group(group)
        print(
            f"  {hour:>10} | {summary['count']:>7} | {summary['win_rate']:>8.1f}% | "
            f"{summary['avg_return']:>+16.4f}%"
        )


def analyze_by_trend_strength(bs, trades, candles, trend_series):
    result = bs.indicators.add_all_indicators(
        candles.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    )
    ema_series = result["ema_14"]

    trend_strengths = []
    for t in trades:
        ema_at_entry = ema_series.asof(t.entry_time)
        if ema_at_entry is None or ema_at_entry == 0:
            continue
        strength_pct = abs(t.entry_price - ema_at_entry) / ema_at_entry * 100
        trend_strengths.append((t, strength_pct))

    if not trend_strengths:
        print("  Could not compute trend strength for any trades.")
        return

    trend_strengths.sort(key=lambda x: x[1])
    n = len(trend_strengths)
    tercile_size = max(1, n // 3)

    weak = [t for t, s in trend_strengths[:tercile_size]]
    medium = [t for t, s in trend_strengths[tercile_size:2 * tercile_size]]
    strong = [t for t, s in trend_strengths[2 * tercile_size:]]

    print(f"  {'Trend Strength':>27} | {'Trades':>7} | {'Win Rate':>9} | {'Avg Return/Trade':>17}")
    print("  " + "-" * 70)
    for label, group in (("WEAK (closest to EMA)", weak), ("MEDIUM", medium), ("STRONG (furthest from EMA)", strong)):
        summary = summarize_group(group)
        if summary:
            print(
                f"  {label:>27} | {summary['count']:>7} | {summary['win_rate']:>8.1f}% | "
                f"{summary['avg_return']:>+16.4f}%"
            )


def main() -> None:
    for symbol in MARKETS_TO_CHECK:
        print("=" * 90)
        print(f"MARKET: {symbol}")
        print("=" * 90)

        try:
            bs = load_market_module(symbol)
            candles = bs.load_candles()
            trend_series = bs.build_trend_series(candles)
            raw_trades = bs.simulate(candles, trend_series)
            trades = [t for t in raw_trades if t.pct_change is not None]
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIPPED: {exc!r}\n")
            continue

        print(f"\n  --- 1. BY PATTERN TYPE ---")
        analyze_by_pattern(trades)

        print(f"\n  --- 2. BY HOUR OF DAY (UTC) ---")
        analyze_by_hour(trades)

        print(f"\n  --- 3. BY TREND STRENGTH AT ENTRY ---")
        analyze_by_trend_strength(bs, trades, candles, trend_series)

        print()


if __name__ == "__main__":
    main()
