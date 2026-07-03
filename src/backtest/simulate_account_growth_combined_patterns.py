"""
simulate_account_growth_combined_patterns.py

Real position-sizing analysis for the COMBINED 5-pattern strategy
(Bullish/Bearish Engulfing, Piercing Line, Three Black Crows, Falling
Three Methods) at S/R retests, on 5min candles -- our best validated
timeframe for this strategy family, confirmed via direct 1min/5min/
15min comparison (5min: +19.936%, 1min: -82.961%, 15min: -5.346%).

Uses the EXACT SAME proven position-sizing methodology as our
original single-pattern strategy's simulation: risk a fixed % of
CURRENT balance per trade, sized using that trade's REAL
initial_stop_distance_pct (not a fixed assumption), compounding
sequentially trade-by-trade.

Run with:
    python src/backtest/simulate_account_growth_combined_patterns.py
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

STARTING_BALANCE = 1000.0
RISK_LEVELS_TO_COMPARE = [0.5, 1.0, 2.0, 5.0]


def simulate_account(trades, starting_balance: float, risk_per_trade_pct: float):
    balance = starting_balance
    balance_history = [balance]

    for trade in trades:
        if trade.pct_change is None or not trade.initial_stop_distance_pct:
            continue

        risk_dollars = balance * (risk_per_trade_pct / 100)
        position_value = risk_dollars / (trade.initial_stop_distance_pct / 100)

        trade_dollar_pnl = position_value * (trade.pct_change / 100)
        balance += trade_dollar_pnl
        balance_history.append(balance)

        if balance <= 0:
            balance = 0
            balance_history[-1] = 0
            break

    return balance, balance_history


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,}")
    print(f"  Date range: {candles_5min.index.min()} to {candles_5min.index.max()}\n")

    trend_series = combined.build_trend_series_for_range(candles_5min)

    print(f"Combined patterns: {list(combined.SELECTED_PATTERNS.keys())}\n")
    print("Simulating combined 5-pattern S/R+Trend strategy on 5min candles ...\n")

    trades = combined.simulate_combined(candles_5min, trend_series)
    decided_trades = [t for t in trades if t.pct_change is not None]
    trades_with_commission = combined.apply_commission(decided_trades, combined.COMMISSION_RATE_PCT)

    total_return = sum(t.pct_change for t in trades_with_commission)
    wins = sum(1 for t in trades_with_commission if t.pct_change > 0)
    win_rate = (wins / len(trades_with_commission) * 100) if trades_with_commission else 0

    print(f"Total decided trades: {len(trades_with_commission):,}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Total % return (spread + commission): {total_return:+.3f}%\n")

    stop_distances = [
        t.initial_stop_distance_pct for t in trades_with_commission if t.initial_stop_distance_pct
    ]
    if stop_distances:
        print(
            f"Initial stop distance range: {min(stop_distances):.4f}% to "
            f"{max(stop_distances):.4f}% (avg {sum(stop_distances)/len(stop_distances):.4f}%)\n"
        )

    print(f"{'Risk/trade':>12} | {'Final balance':>16} | {'Total return':>14} | {'Max drawdown':>14}")
    print("-" * 64)

    for risk_pct in RISK_LEVELS_TO_COMPARE:
        final_balance, history = simulate_account(trades_with_commission, STARTING_BALANCE, risk_pct)

        total_return_pct = (final_balance / STARTING_BALANCE - 1) * 100

        peak = STARTING_BALANCE
        max_drawdown_pct = 0.0
        for b in history:
            peak = max(peak, b)
            if peak > 0:
                drawdown = (peak - b) / peak * 100
                max_drawdown_pct = max(max_drawdown_pct, drawdown)

        print(
            f"{risk_pct:>11.1f}% | ${final_balance:>15,.2f} | "
            f"{total_return_pct:>+13.1f}% | {max_drawdown_pct:>13.1f}%"
        )

    print(
        f"\nStarting balance: ${STARTING_BALANCE:,.2f} | "
        f"Trades simulated: {len(trades_with_commission):,} | "
        f"Period: ~1 year of 5min data"
    )
    print(
        "\nFor comparison, our ORIGINAL single-pattern (Engulfing only)\n"
        "strategy's simulation results (same methodology, same period):\n"
        f"{'Risk/trade':>12} | {'Final balance':>16} | {'Total return':>14} | {'Max drawdown':>14}\n"
        f"{'0.5%':>12} | {'$1,525.29':>16} | {'+52.5%':>14} | {'16.1%':>14}\n"
        f"{'1.0%':>12} | {'$2,212.06':>16} | {'+121.2%':>14} | {'30.3%':>14}\n"
        f"{'2.0%':>12} | {'$4,019.26':>16} | {'+301.9%':>14} | {'53.4%':>14}\n"
        f"{'5.0%':>12} | {'$8,060.35':>16} | {'+706.0%':>14} | {'88.9%':>14}"
    )
    print(
        "\nIMPORTANT CAVEATS:\n"
        "- This is ONE continuous historical period -- not multiple independent years.\n"
        "- Max drawdown shown is what ALREADY HAPPENED in this sample; a longer\n"
        "  live run could see a larger drawdown than anything seen here.\n"
        "- Real execution will have slippage beyond the modeled spread.\n"
        "- Spread/commission rates are based on V25 CFD trading observed live\n"
        "  at one point in time -- these can change.\n"
        "- This combined strategy adds 4 patterns we have NOT yet run through\n"
        "  the same live-bot logic verification we did for the Engulfing-only\n"
        "  strategy -- that step is still needed before trusting this enough\n"
        "  to go live, even on demo."
    )


if __name__ == "__main__":
    main()
