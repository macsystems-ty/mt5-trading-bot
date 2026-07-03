"""
check_compounded_small_windows.py

Properly compounds (not just raw-sums) the last 100, 50, and 20
trades for age_cap=200 vs our current age_cap=500, starting fresh
from $10,000 each time, to check whether age_cap=200's apparently
stronger raw percentage sums hold up under real compounding -- the
same check that revealed age_cap=200's "last 500" raw sum advantage
did NOT hold up (it actually underperformed once compounded properly).

Run with:
    python src/backtest/check_compounded_small_windows.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy"))

import backtester_trend_pullback_v2 as bt
import backtester_sr_patterns_combined as combined
import compare_level_age_caps as age_caps

STARTING_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0
WINDOW_SIZES = [100, 50, 20]
AGE_CAPS_TO_CHECK = [200, 500]


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

    print(f"{'Age Cap':>8} | {'Window':>7} | {'Trades':>7} | {'$10,000 ->':>12} | {'Return':>9}")
    print("-" * 55)

    for cap in AGE_CAPS_TO_CHECK:
        trades = age_caps.simulate(candles_5min, trend_series, cap)
        decided = [t for t in trades if t.pct_change is not None]
        trades_c = age_caps.apply_commission(decided, combined.COMMISSION_RATE_PCT)

        for n in WINDOW_SIZES:
            balance, count = compound(trades_c, n)
            ret_pct = (balance / STARTING_BALANCE - 1) * 100
            print(f"{cap:>8} | {n:>7} | {count:>7} | ${balance:>10,.2f} | {ret_pct:>+8.1f}%")
        print()


if __name__ == "__main__":
    main()
