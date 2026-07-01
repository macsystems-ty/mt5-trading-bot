"""
analyze_breakeven_n2_batches.py

Same batch-of-10 win/loss and balance analysis, but using the
breakeven-then-close-on-counter-candle exit strategy with N=2
favorable candles required before activation (the configuration that
tested best: 53.8% win rate, +84.2% full-history return, +3.4% on
the most recent 500 trades -- vs N=1's recent-trades LOSS).

Run with:
    python src/backtest/analyze_breakeven_n2_batches.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_patterns_combined as combined  # noqa: E402
import compare_breakeven_activation_thresholds as breakeven_n  # noqa: E402

NUM_FAVORABLE_CANDLES_REQUIRED = 2
STARTING_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0
BATCH_SIZE = 10
NUM_TRADES_TO_ANALYZE = 500


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print(
        f"Running BREAKEVEN (N={NUM_FAVORABLE_CANDLES_REQUIRED} favorable candles) "
        f"-> CLOSE-ON-COUNTER-CANDLE exit strategy backtest ...\n"
    )

    all_trades = breakeven_n.simulate_breakeven_then_close(
        candles_5min, trend_series, NUM_FAVORABLE_CANDLES_REQUIRED
    )
    decided_trades = [t for t in all_trades if t.pct_change is not None]
    trades_with_commission = breakeven_n.apply_commission(decided_trades, combined.COMMISSION_RATE_PCT)

    print(f"Total trades in full backtest: {len(trades_with_commission):,}")

    if len(trades_with_commission) < NUM_TRADES_TO_ANALYZE:
        print(
            f"\nWARNING: only {len(trades_with_commission)} trades available, "
            f"fewer than the requested {NUM_TRADES_TO_ANALYZE}. Using all available trades."
        )
        last_n_trades = trades_with_commission
    else:
        last_n_trades = trades_with_commission[-NUM_TRADES_TO_ANALYZE:]

    print(f"Analyzing the last {len(last_n_trades)} trades, grouped into batches of {BATCH_SIZE}.\n")

    balance = STARTING_BALANCE
    last_n_balances = []

    for trade in last_n_trades:
        if not trade.initial_stop_distance_pct:
            last_n_balances.append(balance)
            continue
        risk_dollars = balance * (RISK_PER_TRADE_PCT / 100)
        position_value = risk_dollars / (trade.initial_stop_distance_pct / 100)
        trade_dollar_pnl = position_value * (trade.pct_change / 100)
        balance += trade_dollar_pnl
        last_n_balances.append(balance)
        if balance <= 0:
            balance = 0

    print(f"Account balance entering this {len(last_n_trades)}-trade window: ${STARTING_BALANCE:,.2f}")
    print(f"Account balance at the end (today): ${last_n_balances[-1]:,.2f}\n")

    print(
        f"{'Batch':>6} | {'Trades':>7} | {'Wins':>5} | {'Losses':>7} | "
        f"{'Win Rate':>9} | {'Balance after batch':>20}"
    )
    print("-" * 70)

    total_wins = 0
    total_losses = 0

    num_batches = (len(last_n_trades) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_num in range(num_batches):
        start_idx = batch_num * BATCH_SIZE
        end_idx = min(start_idx + BATCH_SIZE, len(last_n_trades))
        batch_trades = last_n_trades[start_idx:end_idx]

        wins = sum(1 for t in batch_trades if t.pct_change > 0)
        losses = sum(1 for t in batch_trades if t.pct_change <= 0)
        win_rate = (wins / len(batch_trades) * 100) if batch_trades else 0

        total_wins += wins
        total_losses += losses

        balance_after_batch = last_n_balances[end_idx - 1]

        print(
            f"{batch_num + 1:>6} | {len(batch_trades):>7} | {wins:>5} | {losses:>7} | "
            f"{win_rate:>8.1f}% | ${balance_after_batch:>18,.2f}"
        )

    print("-" * 70)
    overall_win_rate = (total_wins / len(last_n_trades) * 100) if last_n_trades else 0
    print(
        f"{'TOTAL':>6} | {len(last_n_trades):>7} | {total_wins:>5} | {total_losses:>7} | "
        f"{overall_win_rate:>8.1f}% |"
    )

    print(
        f"\nStarting balance (this {len(last_n_trades)}-trade window): ${STARTING_BALANCE:,.2f}"
        f"\nRisk per trade: {RISK_PER_TRADE_PCT}%"
        f"\nFinal balance (after these {len(last_n_trades)} trades): "
        f"${last_n_balances[-1]:,.2f}"
        f"\n\n(Note: {len(trades_with_commission):,} trades exist in the full backtest history;\n"
        f"this analysis covers only the most recent {len(last_n_trades)} of them, "
        f"simulated fresh from ${STARTING_BALANCE:,.0f}.)"
    )

    print(
        "\nFor comparison:\n"
        "  Current strategy (trailing-window exit), last 500 trades: "
        "$10,000 -> $13,522.72 (+35.2%)\n"
        "  Breakeven exit N=1, last 500 trades: $10,000 -> $8,974.54 (-10.25%, a real loss)"
    )


if __name__ == "__main__":
    main()
