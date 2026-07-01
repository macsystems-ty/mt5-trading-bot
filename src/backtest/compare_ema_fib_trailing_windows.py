"""
compare_ema_fib_trailing_windows.py

Tests multiple TRAILING_WINDOW sizes for the EMA5/EMA13 + Fibonacci
pullback strategy, since the Phase 1 diagnostic showed losses closing
3x faster than wins (3.5 vs 11.5 candles) -- a strong signal the
3-candle trailing stop may be too tight for this entry style.

Run with:
    python src/backtest/compare_ema_fib_trailing_windows.py
"""

import os
import sys
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WINDOWS_TO_TEST = [3, 5, 7, 10, 15, 20, 25, 30]


def main() -> None:
    print(f"{'Window':>8} | {'Trades':>7} | {'Win Rate':>9} | {'Return':>10} | {'Avg Win Hold':>13} | {'Avg Loss Hold':>14}")
    print("-" * 75)

    for window in WINDOWS_TO_TEST:
        os.environ["EMA_FIB_TRAILING_WINDOW"] = str(window)

        if "backtester_ema_fib" in sys.modules:
            importlib.reload(sys.modules["backtester_ema_fib"])
            import backtester_ema_fib as bef
        else:
            import backtester_ema_fib as bef

        df = bef.load_candles("1min")
        trades = bef.simulate(df)
        decided = [t for t in trades if t.pct_change is not None]

        if not decided:
            print(f"{window:>8} | {'(no trades)':>7} | {'':>9} | {'':>10} | {'':>13} | {'':>14}")
            continue

        trades_with_commission = bef.apply_commission(decided, bef.COMMISSION_RATE_PCT)

        wins = [t for t in trades_with_commission if t.pct_change > 0]
        losses = [t for t in trades_with_commission if t.pct_change <= 0]

        total_return = sum(t.pct_change for t in trades_with_commission)
        win_rate = len(wins) / len(trades_with_commission) * 100

        win_holds = [t.candles_held for t in wins if t.candles_held is not None]
        loss_holds = [t.candles_held for t in losses if t.candles_held is not None]
        avg_win_hold = sum(win_holds) / len(win_holds) if win_holds else 0
        avg_loss_hold = sum(loss_holds) / len(loss_holds) if loss_holds else 0

        print(
            f"{window:>8} | {len(trades_with_commission):>7} | {win_rate:>8.1f}% | "
            f"{total_return:>+9.3f}% | {avg_win_hold:>13.1f} | {avg_loss_hold:>14.1f}"
        )


if __name__ == "__main__":
    main()