"""
check_all_markets_small_windows.py

Applies the same multi-window scrutiny used for 1HZ25V (last 100,
50, and 20 trades, PROPERLY COMPOUNDED) to every market that showed
a profitable full-history backtest, to verify those results hold up
on more recent data.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python check_all_markets_small_windows.py
"""

import importlib.util
import os

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))

MARKETS_TO_CHECK = [
    "1HZ15V", "1HZ25V", "1HZ30V", "1HZ50V",
    "1HZ75V", "1HZ90V", "1HZ100V",
    "R_25", "R_75", "R_100",
]

WINDOW_SIZES = [100, 50, 20]
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
    for t in relevant:
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance, len(relevant)


def main() -> None:
    print(f"{'Market':>10} | {'Window':>7} | {'Trades':>7} | {'$10,000 ->':>14} | {'Return':>9}")
    print("-" * 62)

    for symbol in MARKETS_TO_CHECK:
        try:
            module = load_market_module(symbol)
        except FileNotFoundError as exc:
            print(f"{symbol:>10} | SKIPPED (data not found: {exc})")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol:>10} | SKIPPED (error loading: {exc!r})")
            continue

        try:
            candles = module.load_candles()
            trend_series = module.build_trend_series(candles)
            trades = module.simulate(candles, trend_series)
            decided = [t for t in trades if t.pct_change is not None]
            trades_c = module.apply_commission(decided)
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol:>10} | SKIPPED (error simulating: {exc!r})")
            continue

        for n in WINDOW_SIZES:
            balance, count = compound(trades_c, n, STARTING_BALANCE, RISK_PER_TRADE_PCT)
            ret_pct = (balance / STARTING_BALANCE - 1) * 100
            print(
                f"{symbol:>10} | {n:>7} | {count:>7} | ${balance:>12,.2f} | {ret_pct:>+8.1f}%"
            )
        print()


if __name__ == "__main__":
    main()
