"""
compare_markets_comprehensive.py

Comprehensive multi-factor comparison across all markets that showed
a profitable full-history backtest: max drawdown, quarter-by-quarter
consistency, trade frequency, simplified Sharpe ratio, and pairwise
correlation between markets' per-trade returns.

Run this FROM INSIDE your markets/ folder:
    cd markets
    python compare_markets_comprehensive.py
"""

import importlib.util
import os
import statistics

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))

MARKETS_TO_CHECK = [
    "1HZ15V", "1HZ25V", "1HZ30V", "1HZ50V",
    "1HZ75V", "1HZ90V", "1HZ100V",
    "R_25", "R_75", "R_100",
]

STARTING_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0
NUM_QUARTERS = 4


def load_market_module(symbol: str):
    path = os.path.join(MARKETS_DIR, symbol, "backtest_strategy.py")
    spec = importlib.util.spec_from_file_location(f"backtest_strategy_{symbol}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_equity_curve(trades, starting_balance, risk_pct):
    balance = starting_balance
    curve = [balance]
    for t in trades:
        if not t.initial_stop_distance_pct:
            curve.append(balance)
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
        curve.append(balance)
    return curve


def max_drawdown_pct(equity_curve):
    peak = equity_curve[0]
    max_dd = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return max_dd


def full_history_compounded_return(trades, starting_balance, risk_pct):
    """
    The mathematically correct full-history compounded return -- NOT
    the same as summing each trade's raw pct_change (which is what
    backtest_strategy.py's "Total return" field reports). For a
    percentage-of-balance risk model, compounding through thousands
    of trades produces a dramatically different (much larger) number
    than the raw sum once growth is significant.
    """
    balance = starting_balance
    for t in trades:
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (risk_pct / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance


def quarter_consistency(trades, starting_balance, risk_pct, num_quarters):
    chunk_size = max(1, len(trades) // num_quarters)
    results = []
    for q in range(num_quarters):
        start = q * chunk_size
        end = start + chunk_size if q < num_quarters - 1 else len(trades)
        chunk = trades[start:end]
        if not chunk:
            continue
        balance = starting_balance
        for t in chunk:
            if not t.initial_stop_distance_pct:
                continue
            risk_dollars = balance * (risk_pct / 100)
            position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
            balance += position_value * (t.pct_change / 100)
            if balance <= 0:
                balance = 0
        ret_pct = (balance / starting_balance - 1) * 100
        results.append(ret_pct)
    return results


def simplified_sharpe(trades):
    returns = [t.pct_change for t in trades if t.pct_change is not None]
    if len(returns) < 2:
        return None
    mean_return = statistics.mean(returns)
    stdev_return = statistics.stdev(returns)
    if stdev_return == 0:
        return None
    return mean_return / stdev_return


def trading_days_span(candles) -> float:
    span = candles.index[-1] - candles.index[0]
    return max(span.total_seconds() / 86400, 1.0)


def main() -> None:
    market_data = {}

    print("Loading and simulating all markets ...\n")
    for symbol in MARKETS_TO_CHECK:
        try:
            module = load_market_module(symbol)
            candles = module.load_candles()
            trend_series = module.build_trend_series(candles)
            trades = module.simulate(candles, trend_series)
            decided = [t for t in trades if t.pct_change is not None]
            trades_c = module.apply_commission(decided)
            market_data[symbol] = {"trades": trades_c, "candles": candles}
            print(f"  {symbol}: {len(trades_c):,} trades loaded OK")
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol}: SKIPPED ({exc!r})")

    print("\n" + "=" * 100)
    print("1. MAX DRAWDOWN + TRADE FREQUENCY + RISK-ADJUSTED RETURN")
    print("=" * 100)
    print(
        f"{'Market':>10} | {'Trades':>7} | {'Trades/day':>10} | {'Max Drawdown':>13} | "
        f"{'Simplified Sharpe':>18} | {'TRUE Compounded Return':>23}"
    )
    print("-" * 95)

    for symbol, data in market_data.items():
        trades = data["trades"]
        equity_curve = build_equity_curve(trades, STARTING_BALANCE, RISK_PER_TRADE_PCT)
        dd = max_drawdown_pct(equity_curve)
        days = trading_days_span(data["candles"])
        trades_per_day = len(trades) / days
        sharpe = simplified_sharpe(trades)
        sharpe_str = f"{sharpe:.4f}" if sharpe is not None else "N/A"
        true_compounded = full_history_compounded_return(trades, STARTING_BALANCE, RISK_PER_TRADE_PCT)
        true_compounded_pct = (true_compounded / STARTING_BALANCE - 1) * 100

        print(
            f"{symbol:>10} | {len(trades):>7,} | {trades_per_day:>10.2f} | {dd:>12.1f}% | "
            f"{sharpe_str:>18} | {true_compounded_pct:>+22.1f}%"
        )

    print("\n" + "=" * 100)
    print(f"2. QUARTER-BY-QUARTER CONSISTENCY ({NUM_QUARTERS} sequential chunks)")
    print("=" * 100)
    print(
        "NOTE: each quarter's % return is mathematically identical whether computed\n"
        "fresh-from-$10k or as that quarter's actual contribution to the continuous\n"
        "full-history run -- this is expected for a percentage-of-balance risk model\n"
        "(chaining the same sequence of % changes gives the same product regardless of\n"
        "starting balance), NOT a sign of a bug. These numbers are NOT directly\n"
        "comparable to the single full-history TRUE COMPOUNDED RETURN above, which\n"
        "reflects the actual multiplicative effect of all quarters chained together.\n"
    )
    header = f"{'Market':>10} |"
    for q in range(NUM_QUARTERS):
        header += f" {'Q' + str(q+1):>10} |"
    header += f" {'# Profitable':>12}"
    print(header)
    print("-" * len(header))

    for symbol, data in market_data.items():
        trades = data["trades"]
        quarter_returns = quarter_consistency(trades, STARTING_BALANCE, RISK_PER_TRADE_PCT, NUM_QUARTERS)
        num_profitable = sum(1 for r in quarter_returns if r > 0)

        row = f"{symbol:>10} |"
        for r in quarter_returns:
            row += f" {r:>+9.1f}% |"
        row += f" {num_profitable}/{len(quarter_returns):>10}"
        print(row)

    print("\n" + "=" * 100)
    print("3. CORRELATION BETWEEN MARKETS (per-trade returns, aligned by trade SEQUENCE)")
    print("=" * 100)
    print(
        "NOTE: trades happen at different real times across markets, so this aligns by\n"
        "trade NUMBER (1st vs 1st, 2nd vs 2nd, etc.), not exact timestamp -- a rough but\n"
        "informative proxy for whether these markets' edges move together.\n"
    )

    symbols = list(market_data.keys())
    return_series = {}
    for symbol in symbols:
        returns = [t.pct_change for t in market_data[symbol]["trades"] if t.pct_change is not None]
        return_series[symbol] = returns

    min_len = min(len(r) for r in return_series.values()) if return_series else 0

    header = f"{'':>10} |"
    for symbol in symbols:
        header += f" {symbol:>8} |"
    print(header)
    print("-" * len(header))

    for symbol_a in symbols:
        row = f"{symbol_a:>10} |"
        series_a = return_series[symbol_a][:min_len]
        for symbol_b in symbols:
            series_b = return_series[symbol_b][:min_len]
            if symbol_a == symbol_b:
                corr = 1.0
            else:
                try:
                    corr = statistics.correlation(series_a, series_b)
                except (statistics.StatisticsError, ValueError):
                    corr = float("nan")
            row += f" {corr:>+8.2f} |"
        print(row)


if __name__ == "__main__":
    main()