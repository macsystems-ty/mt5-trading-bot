"""
backtest_strategy.py  --  1HZ100V

Our FULLY VALIDATED strategy (originally proven on 1HZ25V), tested
here on a different market. All strategy parameters are listed
explicitly below for clarity.

Reuses our proven, shared strategy modules (candlestick pattern
detection, indicators, swing-level detection) from
../../src/strategy and ../../src/backtest, rather than duplicating
their logic.

Run with:
    python backtest_strategy.py
(after running fetch_historical.py first to populate data/)
"""

import os
import sys

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src", "backtest"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src", "strategy"))

import backtester_sr_engulfing as sre  # noqa: E402
import indicators  # noqa: E402
from candlestick_patterns import (  # noqa: E402
    Candle,
    is_bullish_engulfing, is_bearish_engulfing,
    is_piercing_line, is_three_black_crows, is_falling_three_methods,
    is_shooting_star, is_morning_star, is_evening_star,
    is_dark_cloud_cover, is_three_white_soldiers,
)

# =====================================================================
# MARKET-SPECIFIC: edit these two for each market folder.
# =====================================================================
SYMBOL = "1HZ100V"
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"{SYMBOL}_5min.csv")

# =====================================================================
# VALIDATED STRATEGY PARAMETERS (proven on 1HZ25V).
# =====================================================================
EMA_TREND_PERIOD = 14
SWING_LOOKBACK = 3
RETEST_TOLERANCE_PCT = 0.05
LEVEL_AGE_CAP = 200
TRAILING_WINDOW = 10
NUM_FAVORABLE_CANDLES_REQUIRED = 2
STAGE3_WINDOW = 2
COMMISSION_RATE_PCT = 0.02 / 160 * 100
STARTING_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0

SELECTED_PATTERNS = {
    "bullish_engulfing": {"candles_needed": 2, "direction": "BUY"},
    "bearish_engulfing": {"candles_needed": 2, "direction": "SELL"},
    "piercing_line": {"candles_needed": 2, "direction": "BUY"},
    "three_black_crows": {"candles_needed": 3, "direction": "SELL"},
    "falling_three_methods": {"candles_needed": 5, "direction": "SELL"},
    "shooting_star": {"candles_needed": 1, "direction": "SELL"},
    "morning_star": {"candles_needed": 3, "direction": "BUY"},
    "evening_star": {"candles_needed": 3, "direction": "SELL"},
    "dark_cloud_cover": {"candles_needed": 2, "direction": "SELL"},
    "three_white_soldiers": {"candles_needed": 3, "direction": "BUY"},
}
MAX_CANDLES_NEEDED = max(p["candles_needed"] for p in SELECTED_PATTERNS.values())
BUY_PATTERNS = [n for n, p in SELECTED_PATTERNS.items() if p["direction"] == "BUY"]
SELL_PATTERNS = [n for n, p in SELECTED_PATTERNS.items() if p["direction"] == "SELL"]


def load_candles() -> pd.DataFrame:
    if not os.path.exists(DATA_PATH):
        raise FileNotFoundError(f"No data found at {DATA_PATH}. Run fetch_historical.py first.")
    df = pd.read_csv(DATA_PATH, parse_dates=["open_time"], index_col="open_time")
    return df.sort_index()


def compute_ema_trend(candles_1h: pd.DataFrame) -> pd.Series:
    result = indicators.add_all_indicators(candles_1h)
    ema_col = f"ema_{EMA_TREND_PERIOD}"
    if ema_col not in result.columns:
        raise ValueError(f"indicators module doesn't have a precomputed '{ema_col}' column.")
    ema = result[ema_col]
    close = result["close"]
    ema_rising = ema.diff() > 0
    price_above = close > ema
    trend = pd.Series("FLAT", index=candles_1h.index)
    trend[price_above & ema_rising] = "UP"
    trend[(~price_above) & (~ema_rising)] = "DOWN"
    return trend


def build_trend_series(candles_5min: pd.DataFrame) -> pd.Series:
    candles_1h = (
        candles_5min.resample("1h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    return compute_ema_trend(candles_1h)


def detect_pattern(pattern_name: str, candles) -> bool:
    if pattern_name == "bullish_engulfing":
        return is_bullish_engulfing(candles[-2], candles[-1])
    if pattern_name == "bearish_engulfing":
        return is_bearish_engulfing(candles[-2], candles[-1])
    if pattern_name == "piercing_line":
        return is_piercing_line(candles[-2], candles[-1])
    if pattern_name == "three_black_crows":
        return is_three_black_crows(candles[-3], candles[-2], candles[-1])
    if pattern_name == "falling_three_methods":
        return is_falling_three_methods(candles[-5:])
    if pattern_name == "shooting_star":
        return is_shooting_star(candles[-1])
    if pattern_name == "morning_star":
        return is_morning_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "evening_star":
        return is_evening_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "dark_cloud_cover":
        return is_dark_cloud_cover(candles[-2], candles[-1])
    if pattern_name == "three_white_soldiers":
        return is_three_white_soldiers(candles[-3], candles[-2], candles[-1])
    raise ValueError(f"Unknown pattern: {pattern_name}")


def any_pattern_matches(direction: str, candles):
    pattern_list = BUY_PATTERNS if direction == "BUY" else SELL_PATTERNS
    for pattern_name in pattern_list:
        needed = SELECTED_PATTERNS[pattern_name]["candles_needed"]
        if len(candles) < needed:
            continue
        if detect_pattern(pattern_name, candles[-needed:]):
            return pattern_name
    return None


class Trade:
    def __init__(self, direction, entry_time, entry_price, matched_pattern, initial_stop_distance_pct):
        self.direction = direction
        self.entry_time = entry_time
        self.entry_price = entry_price
        self.matched_pattern = matched_pattern
        self.initial_stop_distance_pct = initial_stop_distance_pct
        self.exit_time = None
        self.exit_price = None
        self.candles_held = None
        self.pct_change = None
        self.exit_reason = None


def is_favorable_candle(open_price, close_price, direction):
    if direction == "BUY":
        return close_price > open_price
    return close_price < open_price


def simulate(df: pd.DataFrame, trend_series: pd.Series):
    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")

    all_levels = sre.identify_swing_levels(df, SWING_LOOKBACK)
    level_becomes_known_at = {lvl["index"] + SWING_LOOKBACK: lvl for lvl in all_levels}
    loop_start = max(SWING_LOOKBACK * 2 + 2, MAX_CANDLES_NEEDED + 1)

    active_levels = [
        dict(lvl, tested_count=0)
        for known_at, lvl in level_becomes_known_at.items()
        if known_at < loop_start
    ]

    trades = []
    open_trade = None
    open_trade_index = None
    stop_loss_price = None
    favorable_candle_count = 0
    stage3_active = False

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open, current_high, current_low, current_close = opens[i], highs[i], lows[i], closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [lvl for lvl in active_levels if i - lvl["index"] <= LEVEL_AGE_CAP]

        if open_trade is not None:
            candles_held = i - open_trade_index
            should_close = False
            exit_level = None
            exit_reason = None

            if stage3_active:
                window_start = max(0, i - STAGE3_WINDOW)
                if open_trade.direction == "BUY":
                    new_stop = min(lows[window_start:i])
                    if new_stop > stop_loss_price:
                        stop_loss_price = new_stop
                    should_close = current_low <= stop_loss_price
                else:
                    new_stop = max(highs[window_start:i])
                    if new_stop < stop_loss_price:
                        stop_loss_price = new_stop
                    should_close = current_high >= stop_loss_price
                if should_close:
                    exit_level = stop_loss_price
                    exit_reason = "stage3_trailing_stop_hit"
            else:
                if open_trade.direction == "BUY":
                    hit_stop = current_low <= stop_loss_price
                else:
                    hit_stop = current_high >= stop_loss_price
                if hit_stop:
                    should_close = True
                    exit_level = stop_loss_price
                    exit_reason = "initial_or_breakeven_stop_hit"

                favorable = is_favorable_candle(current_open, current_close, open_trade.direction)
                if favorable:
                    favorable_candle_count += 1
                    if favorable_candle_count >= NUM_FAVORABLE_CANDLES_REQUIRED:
                        stop_loss_price = open_trade.entry_price
                        stage3_active = True
                else:
                    favorable_candle_count = 0

            if should_close:
                exit_price = sre.exit_fill_price(exit_level, open_trade.direction)
                entry_price = open_trade.entry_price
                if open_trade.direction == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100

                open_trade.exit_time = current_time
                open_trade.exit_price = exit_price
                open_trade.pct_change = pct_change
                open_trade.candles_held = candles_held
                open_trade.exit_reason = exit_reason

                trades.append(open_trade)
                open_trade = None
                open_trade_index = None
                stop_loss_price = None
                favorable_candle_count = 0
                stage3_active = False

            continue

        if trend not in ("UP", "DOWN"):
            continue

        direction = "BUY" if trend == "UP" else "SELL"
        required_level_type = "support" if direction == "BUY" else "resistance"

        for level in active_levels:
            if level["type"] != required_level_type:
                continue

            level_price = level["price"]
            tolerance = level_price * (RETEST_TOLERANCE_PCT / 100)
            retest_price = lows[i - 1] if level["type"] == "support" else highs[i - 1]
            if not (level_price - tolerance <= retest_price <= level_price + tolerance):
                continue

            level["tested_count"] += 1

            recent_candles = [
                Candle(open=opens[j], high=highs[j], low=lows[j], close=closes[j])
                for j in range(i - MAX_CANDLES_NEEDED + 1, i + 1)
            ]
            matched_pattern = any_pattern_matches(direction, recent_candles)
            if matched_pattern is None:
                continue

            fill_price = sre.entry_fill_price(current_close, direction)
            window_start = max(0, i - TRAILING_WINDOW + 1)
            initial_stop = (
                min(lows[window_start: i + 1]) if direction == "BUY"
                else max(highs[window_start: i + 1])
            )
            stop_distance_pct = abs(fill_price - initial_stop) / fill_price * 100

            open_trade = Trade(direction, current_time, fill_price, matched_pattern, stop_distance_pct)
            open_trade_index = i
            stop_loss_price = initial_stop
            favorable_candle_count = 0
            stage3_active = False
            break

    return trades


def apply_commission(trades):
    for t in trades:
        if t.pct_change is not None:
            t.pct_change -= COMMISSION_RATE_PCT
    return trades


def compound(trades, n=None):
    relevant = trades[-n:] if n else trades
    balance = STARTING_BALANCE
    for t in relevant:
        if not t.initial_stop_distance_pct:
            continue
        risk_dollars = balance * (RISK_PER_TRADE_PCT / 100)
        position_value = risk_dollars / (t.initial_stop_distance_pct / 100)
        balance += position_value * (t.pct_change / 100)
        if balance <= 0:
            balance = 0
    return balance


def main():
    print(f"Loading 5min candles for {SYMBOL} ...")
    candles = load_candles()
    print(f"  Candles: {len(candles):,} (range: {candles.index[0]} to {candles.index[-1]})\n")

    trend_series = build_trend_series(candles)
    trades = simulate(candles, trend_series)
    decided = [t for t in trades if t.pct_change is not None]
    trades_c = apply_commission(decided)

    if not trades_c:
        print("No trades generated -- check data and parameters.")
        return

    wins = [t for t in trades_c if t.pct_change > 0]
    total_return = sum(t.pct_change for t in trades_c)
    win_rate = len(wins) / len(trades_c) * 100

    last_500_balance = compound(trades_c, 500)

    print("=" * 70)
    print(f"RESULT for {SYMBOL}")
    print("=" * 70)
    print(f"Total trades: {len(trades_c):,}")
    print(f"Win rate:     {win_rate:.1f}%")
    print(f"Total return: {total_return:+.3f}%")
    print(f"Last 500 trades, compounded from ${STARTING_BALANCE:,.0f}: ${last_500_balance:,.2f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
