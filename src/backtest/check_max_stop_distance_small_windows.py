"""
check_max_stop_distance_small_windows.py

Properly compounds the last 100, 50, and 20 trades for max-stop-
distance thresholds 0.16, 0.18, 0.20, 0.22 vs our current no-filter
baseline, to resolve the noisy signal seen in the single last-500
check.

Run with:
    python src/backtest/check_max_stop_distance_small_windows.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy"))

import backtester_trend_pullback_v2 as bt
import backtester_sr_patterns_combined as combined
import compare_max_stop_distance_filter as max_stop

STARTING_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0
WINDOW_SIZES = [100, 50, 20]
THRESHOLDS_TO_CHECK = [None, 0.22, 0.20, 0.18, 0.16]


def compound(trades, n):
    last_n = trades[-n:] if len(trades) >= n else trades
    balance = STARTING_BALANCE
    for t in last_n:
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (RISK_PER_TRADE_PCT / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance, len(last_n)


def main():
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print(f"{'Max Stop %':>10} | {'Window':>7} | {'Trades':>7} | {'$10,000 ->':>12} | {'Return':>9}")
    print("-" * 57)

    for threshold in THRESHOLDS_TO_CHECK:
        trades = max_stop.simulate(candles_5min, trend_series, 2, 2, threshold)
        decided = [t for t in trades if t.pct_change is not None]
        trades_c = max_stop.apply_commission(decided, combined.COMMISSION_RATE_PCT)

        label = "None" if threshold is None else f"{threshold}"
        for n in WINDOW_SIZES:
            balance, count = compound(trades_c, n)
            ret_pct = (balance / STARTING_BALANCE - 1) * 100
            print(f"{label:>10} | {n:>7} | {count:>7} | ${balance:>10,.2f} | {ret_pct:>+8.1f}%")
        print()


if __name__ == "__main__":
    main()
