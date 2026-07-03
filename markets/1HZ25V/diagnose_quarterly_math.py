"""
diagnose_quarterly_math.py

Traces exactly why quarter-RESET compounding (each quarter starting
fresh at $10,000) can produce much larger PERCENTAGE returns than
continuous full-history compounding on the SAME underlying trades.

Run this FROM INSIDE the specific market folder you want to check:
    cd markets/1HZ25V
    python ../diagnose_quarterly_math.py
"""

import os
import sys

sys.path.insert(0, os.getcwd())

import backtest_strategy as bs  # noqa: E402

STARTING_BALANCE = 10000.0
RISK_PCT = 1.0
NUM_QUARTERS = 4


def compound_chunk(trades, starting_balance):
    balance = starting_balance
    position_values = []
    for t in trades:
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (RISK_PCT / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        position_values.append(position_value)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance, position_values


def main() -> None:
    print(f"Loading 5min candles for {bs.SYMBOL} ...")
    candles = bs.load_candles()
    trend_series = bs.build_trend_series(candles)
    trades = bs.simulate(candles, trend_series)
    decided = [t for t in trades if t.pct_change is not None]
    trades_c = bs.apply_commission(decided)

    print(f"Total trades: {len(trades_c):,}\n")

    chunk_size = max(1, len(trades_c) // NUM_QUARTERS)
    continuous_balance = STARTING_BALANCE
    continuous_boundary_balances = [STARTING_BALANCE]

    for i, t in enumerate(trades_c):
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = continuous_balance * (RISK_PCT / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        continuous_balance += position_value * (t.pct_change / 100)
        if continuous_balance <= 0:
            continuous_balance = 0

        if (i + 1) % chunk_size == 0:
            continuous_boundary_balances.append(continuous_balance)

    if len(continuous_boundary_balances) < NUM_QUARTERS + 1:
        continuous_boundary_balances.append(continuous_balance)

    print("=" * 100)
    print("PER-QUARTER COMPARISON")
    print("=" * 100)
    print(
        f"{'Quarter':>8} | {'Trades':>7} | {'Win%':>6} | {'Avg Pos $ (fresh)':>18} | "
        f"{'Avg Pos $ (continuous)':>22} | {'Fresh-reset Return':>19} | "
        f"{'Continuous contribution':>24}"
    )
    print("-" * 115)

    for q in range(NUM_QUARTERS):
        start = q * chunk_size
        end = start + chunk_size if q < NUM_QUARTERS - 1 else len(trades_c)
        chunk = trades_c[start:end]
        if not chunk:
            continue

        wins = sum(1 for t in chunk if t.pct_change > 0)
        win_rate = wins / len(chunk) * 100

        fresh_final, fresh_positions = compound_chunk(chunk, STARTING_BALANCE)
        fresh_ret = (fresh_final / STARTING_BALANCE - 1) * 100
        avg_fresh_pos = sum(fresh_positions) / len(fresh_positions) if fresh_positions else 0

        continuous_start_balance = continuous_boundary_balances[q]
        continuous_end_balance = continuous_boundary_balances[q + 1]
        _, continuous_positions = compound_chunk(chunk, continuous_start_balance)
        avg_continuous_pos = (
            sum(continuous_positions) / len(continuous_positions) if continuous_positions else 0
        )
        continuous_contribution_pct = (
            (continuous_end_balance / continuous_start_balance - 1) * 100
            if continuous_start_balance > 0 else 0
        )

        print(
            f"Q{q+1:>7} | {len(chunk):>7} | {win_rate:>5.1f}% | ${avg_fresh_pos:>16,.2f} | "
            f"${avg_continuous_pos:>20,.2f} | {fresh_ret:>+18.1f}% | {continuous_contribution_pct:>+23.1f}%"
        )

    print(
        f"\nCONTINUOUS full-history final balance: ${continuous_balance:,.2f} "
        f"({(continuous_balance/STARTING_BALANCE-1)*100:+.1f}%)"
    )
    print(
        "\nKEY INSIGHT: compare 'Avg Pos $ (fresh)' vs 'Avg Pos $ (continuous)' for each\n"
        "quarter. If continuous position sizes are MUCH larger (because the account has\n"
        "grown a lot by that point), the SAME percentage-based trades produce smaller\n"
        "PERCENTAGE swings relative to that larger base in later quarters of the\n"
        "continuous run, while a fresh $10,000 reset lets the same trades swing the\n"
        "(smaller) balance by a much larger PERCENTAGE -- this is a real mathematical\n"
        "effect of resetting the base each quarter, not necessarily a bug, but it means\n"
        "the per-quarter fresh-reset percentages are NOT simply comparable to the single\n"
        "continuous full-history percentage."
    )


if __name__ == "__main__":
    main()
