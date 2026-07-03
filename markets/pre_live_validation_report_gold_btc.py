"""
pre_live_validation_report.py

Comprehensive pre-live-trading validation report for our chosen
Tier-1 markets (R_100, 1HZ75V, 1HZ90V, 1HZ100V), showing for each
market AND each recent-window size (500, 200, 100, 50, 20 trades):
trades, wins, losses, win rate, and properly COMPOUNDED dollar
outcome starting fresh from $10,000.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python pre_live_validation_report.py
"""

import importlib.util
import os

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))

MARKETS_TO_CHECK = ["XAUUSD", "BTCUSD"]
WINDOW_SIZES = [500, 200, 100, 50, 20]
STARTING_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compound(trades, n, starting_balance, risk_pct):
    relevant = trades[-n:] if len(trades) >= n else trades
    balance = starting_balance
    wins = 0
    losses = 0
    for t in relevant:
        if t.pct_change > 0:
            wins += 1
        else:
            losses += 1
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance, len(relevant), wins, losses


def main() -> None:
    print("=" * 100)
    print("PRE-LIVE VALIDATION REPORT")
    print("=" * 100)
    print(
        f"Markets: {', '.join(MARKETS_TO_CHECK)}\n"
        f"Starting balance per window: ${STARTING_BALANCE:,.0f}\n"
        f"Risk per trade: {RISK_PER_TRADE_PCT}%\n"
    )

    for symbol in MARKETS_TO_CHECK:
        print("=" * 100)
        print(f"MARKET: {symbol}")
        print("=" * 100)

        try:
            module = load_market_module(symbol)
            candles = module.load_candles()
            trend_series = module.build_trend_series(candles)
            trades = module.simulate(candles, trend_series)
            decided = [t for t in trades if t.pct_change is not None]
            trades_c = module.apply_commission(decided)
        except FileNotFoundError as exc:
            print(f"  SKIPPED: data not found ({exc})\n")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIPPED: error ({exc!r})\n")
            continue

        print(f"  Total trades available (full history): {len(trades_c):,}\n")
        print(
            f"  {'Window':>8} | {'Trades':>7} | {'Wins':>6} | {'Losses':>7} | "
            f"{'Win Rate':>9} | {'$10,000 ->':>14} | {'Return':>9}"
        )
        print("  " + "-" * 78)

        for n in WINDOW_SIZES:
            balance, count, wins, losses = compound(
                trades_c, n, STARTING_BALANCE, RISK_PER_TRADE_PCT
            )
            win_rate = (wins / count * 100) if count else 0
            ret_pct = (balance / STARTING_BALANCE - 1) * 100
            print(
                f"  {n:>8} | {count:>7} | {wins:>6} | {losses:>7} | {win_rate:>8.1f}% | "
                f"${balance:>12,.2f} | {ret_pct:>+8.1f}%"
            )
        print()


if __name__ == "__main__":
    main()
