"""
test_alternate_timeframes.py

STEP 1 of the timeframe exploration: resamples existing 5-minute
candle data into 10, 15, 20, 25, and 30-minute candles, then runs
the UNMODIFIED strategy at each timeframe.

IMPORTANT: all candle-count parameters are kept UNCHANGED for this
first pass -- they will mean a different real TIME SPAN at each
timeframe. We're first checking whether ANY of these timeframes show
promise using the strategy exactly as-is.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python test_alternate_timeframes.py
"""

import importlib.util
import os

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))
MARKETS_TO_CHECK = ["1HZ25V", "1HZ75V", "1HZ90V", "1HZ100V", "R_100"]
TIMEFRAMES_TO_TEST_MINUTES = [10, 15, 20, 25, 30]

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resample_candles(candles_5min, target_minutes):
    rule = f"{target_minutes}min"
    resampled = candles_5min.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }).dropna()
    return resampled


def compound(trades, starting_balance, risk_pct):
    balance = starting_balance
    for t in trades:
        if t.pct_change is None or not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance


def compound_windowed(trades, window_size, starting_balance, risk_pct):
    decided = [t for t in trades if t.pct_change is not None]
    results = []
    for start in range(0, len(decided), window_size):
        chunk = decided[start:start + window_size]
        if not chunk:
            continue
        balance = compound(chunk, starting_balance, risk_pct)
        ret_pct = (balance / starting_balance - 1) * 100
        results.append({"return_pct": ret_pct})
    return results


def main() -> None:
    for symbol in MARKETS_TO_CHECK:
        print("=" * 100)
        print(f"MARKET: {symbol}")
        print("=" * 100)

        try:
            bs = load_market_module(symbol)
            candles_5min = bs.load_candles()
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIPPED: {exc!r}\n")
            continue

        trend_series_5min = bs.build_trend_series(candles_5min)
        baseline_trades = bs.simulate(candles_5min, trend_series_5min)
        baseline_decided = [t for t in baseline_trades if t.pct_change is not None]
        baseline_balance = compound(baseline_decided, STARTING_BALANCE, RISK_PCT)
        baseline_ret = (baseline_balance / STARTING_BALANCE - 1) * 100
        baseline_wins = sum(1 for t in baseline_decided if t.pct_change > 0)
        baseline_win_rate = baseline_wins / len(baseline_decided) * 100 if baseline_decided else 0

        print(f"  {'Timeframe':>15} | {'Trades':>7} | {'Win Rate':>9} | {'Compounded Return':>18}")
        print("  " + "-" * 60)
        print(f"  {'5min (baseline)':>15} | {len(baseline_decided):>7} | {baseline_win_rate:>8.1f}% | {baseline_ret:>+17.1f}%")

        results_for_windows = {"5min (baseline)": baseline_decided}

        for target_minutes in TIMEFRAMES_TO_TEST_MINUTES:
            try:
                resampled = resample_candles(candles_5min, target_minutes)
                trend_series = bs.build_trend_series(resampled)
                trades = bs.simulate(resampled, trend_series)
                decided = [t for t in trades if t.pct_change is not None]
            except Exception as exc:  # noqa: BLE001
                print(f"  {f'{target_minutes}min':>15} | SKIPPED: {exc!r}")
                continue

            balance = compound(decided, STARTING_BALANCE, RISK_PCT)
            ret = (balance / STARTING_BALANCE - 1) * 100
            wins = sum(1 for t in decided if t.pct_change > 0)
            win_rate = wins / len(decided) * 100 if decided else 0

            label = f"{target_minutes}min"
            print(f"  {label:>15} | {len(decided):>7} | {win_rate:>8.1f}% | {ret:>+17.1f}%")
            results_for_windows[label] = decided

        print(f"\n  --- LAST 100 TRADES, 20-trade windows ---")
        baseline_windows = compound_windowed(baseline_decided, 20, STARTING_BALANCE, RISK_PCT)
        baseline_windows = baseline_windows[-5:]
        print(f"    {'Timeframe':>15} | " + " | ".join(f"W{i+1:>6}" for i in range(len(baseline_windows))))
        row = f"    {'5min (baseline)':>15} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in baseline_windows)
        print(row)

        for label, decided in results_for_windows.items():
            if label == "5min (baseline)":
                continue
            windows = compound_windowed(decided, 20, STARTING_BALANCE, RISK_PCT)
            windows = windows[-5:]
            row = f"    {label:>15} | " + " | ".join(f"{w['return_pct']:>+6.1f}%" for w in windows)
            print(row)

        print()


if __name__ == "__main__":
    main()
