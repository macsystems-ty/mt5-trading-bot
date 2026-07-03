"""
backtester_candlestick_patterns.py

Tests each of the 12 directional candlestick patterns SEPARATELY,
paired with our VALIDATED EMA(50) on 1H candles trend filter (proven
not to be noisy, unlike the EMA5/13 we tested and rejected earlier).

A pattern only triggers an entry when it agrees with the current
trend direction (e.g. Hammer only triggers a BUY when trend=UP).

Exit: fixed 1:2 Risk:Reward (stop-loss at a fixed % distance, take-
profit at 2x that distance), per the user's request to size risk in
dollar terms (e.g. 5% of account = the "1" unit of risk) rather than
deriving the stop from recent candle noise -- the latter approach was
found to produce stops SMALLER than the spread cost itself for
certain patterns (e.g. Hammer, whose own long wick sits close to the
trailing-stop window's low).

Tests on BOTH 1min and 5min timeframes, using the full real dataset
(6+ months), per our updated policy of never trusting small samples
again.

Run with:
    python src/backtest/backtester_candlestick_patterns.py
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

from candlestick_patterns import (  # noqa: E402
    Candle,
    is_bullish_engulfing, is_bearish_engulfing,
    is_hammer, is_inverted_hammer, is_shooting_star,
    is_morning_star, is_evening_star,
    is_piercing_line, is_dark_cloud_cover,
    is_three_white_soldiers, is_three_black_crows,
    is_rising_three_methods, is_falling_three_methods,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOL = "1HZ25V"

STOP_LOSS_PCT = float(os.getenv("PATTERN_STOP_LOSS_PCT", "0.05"))
RISK_REWARD_RATIO = float(os.getenv("PATTERN_RISK_REWARD_RATIO", "2.0"))
TAKE_PROFIT_PCT = STOP_LOSS_PCT * RISK_REWARD_RATIO

SPREAD_PCT = 58 / 849362
HALF_SPREAD_PCT = SPREAD_PCT / 2
COMMISSION_RATE_PCT = 0.02 / 160 * 100


def load_candles(timeframe: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"{SYMBOL}_{timeframe}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing {path}.")
    return pd.read_csv(path, index_col="open_time", parse_dates=True).sort_index()


def entry_fill_price(mid_price: float, direction: str) -> float:
    return mid_price * (1 + HALF_SPREAD_PCT if direction == "BUY" else 1 - HALF_SPREAD_PCT)


def exit_fill_price(mid_price: float, direction: str) -> float:
    return mid_price * (1 - HALF_SPREAD_PCT if direction == "BUY" else 1 + HALF_SPREAD_PCT)


def compute_ema50_1h_trend(candles_1h: pd.DataFrame) -> pd.Series:
    close = candles_1h["close"]
    ema = close.ewm(span=50, adjust=False).mean()
    ema_rising = ema.diff() > 0
    price_above = close > ema

    trend = pd.Series("FLAT", index=candles_1h.index)
    trend[price_above & ema_rising] = "UP"
    trend[(~price_above) & (~ema_rising)] = "DOWN"
    trend.iloc[0] = "FLAT"
    return trend


def align_trend_to_entry_timeframe(trend_1h: pd.Series, entry_index: pd.DatetimeIndex) -> pd.Series:
    return trend_1h.reindex(entry_index, method="ffill")


PATTERN_DEFS = {
    "bullish_engulfing": {"candles_needed": 2, "direction": "BUY"},
    "bearish_engulfing": {"candles_needed": 2, "direction": "SELL"},
    "hammer": {"candles_needed": 1, "direction": "BUY"},
    "inverted_hammer": {"candles_needed": 1, "direction": "BUY"},
    "shooting_star": {"candles_needed": 1, "direction": "SELL"},
    "morning_star": {"candles_needed": 3, "direction": "BUY"},
    "evening_star": {"candles_needed": 3, "direction": "SELL"},
    "piercing_line": {"candles_needed": 2, "direction": "BUY"},
    "dark_cloud_cover": {"candles_needed": 2, "direction": "SELL"},
    "three_white_soldiers": {"candles_needed": 3, "direction": "BUY"},
    "three_black_crows": {"candles_needed": 3, "direction": "SELL"},
    "rising_three_methods": {"candles_needed": 5, "direction": "BUY"},
    "falling_three_methods": {"candles_needed": 5, "direction": "SELL"},
}


def detect_pattern(pattern_name: str, candles: List[Candle]) -> bool:
    if pattern_name == "bullish_engulfing":
        return is_bullish_engulfing(candles[-2], candles[-1])
    if pattern_name == "bearish_engulfing":
        return is_bearish_engulfing(candles[-2], candles[-1])
    if pattern_name == "hammer":
        return is_hammer(candles[-1])
    if pattern_name == "inverted_hammer":
        return is_inverted_hammer(candles[-1])
    if pattern_name == "shooting_star":
        return is_shooting_star(candles[-1])
    if pattern_name == "morning_star":
        return is_morning_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "evening_star":
        return is_evening_star(candles[-3], candles[-2], candles[-1])
    if pattern_name == "piercing_line":
        return is_piercing_line(candles[-2], candles[-1])
    if pattern_name == "dark_cloud_cover":
        return is_dark_cloud_cover(candles[-2], candles[-1])
    if pattern_name == "three_white_soldiers":
        return is_three_white_soldiers(candles[-3], candles[-2], candles[-1])
    if pattern_name == "three_black_crows":
        return is_three_black_crows(candles[-3], candles[-2], candles[-1])
    if pattern_name == "rising_three_methods":
        return is_rising_three_methods(candles[-5:])
    if pattern_name == "falling_three_methods":
        return is_falling_three_methods(candles[-5:])
    raise ValueError(f"Unknown pattern: {pattern_name}")


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    pct_change: Optional[float] = None
    candles_held: Optional[int] = None
    exit_reason: Optional[str] = None


def simulate_pattern(df: pd.DataFrame, trend: pd.Series, pattern_name: str) -> List[Trade]:
    pattern_def = PATTERN_DEFS[pattern_name]
    needed = pattern_def["candles_needed"]
    pattern_direction = pattern_def["direction"]
    required_trend = "UP" if pattern_direction == "BUY" else "DOWN"

    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df.index

    trend_aligned = align_trend_to_entry_timeframe(trend, times)

    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    open_trade_index: Optional[int] = None

    start = needed + 1

    for i in range(start, len(df)):
        current_trend = trend_aligned.iloc[i]

        if open_trade is not None:
            current_high = highs[i]
            current_low = lows[i]

            if open_trade.direction == "BUY":
                hit_sl = current_low <= open_trade.stop_loss_price
                hit_tp = current_high >= open_trade.take_profit_price
            else:
                hit_sl = current_high >= open_trade.stop_loss_price
                hit_tp = current_low <= open_trade.take_profit_price

            # If both SL and TP are touched within the SAME candle, we
            # conservatively assume SL hit first (worst-case assumption,
            # since we can't know intra-candle order from OHLC alone).
            if hit_sl or hit_tp:
                if hit_sl:
                    exit_level = open_trade.stop_loss_price
                    exit_reason = "stop_loss"
                else:
                    exit_level = open_trade.take_profit_price
                    exit_reason = "take_profit"

                exit_price = exit_fill_price(exit_level, open_trade.direction)
                if open_trade.direction == "BUY":
                    pct_change = (exit_price - open_trade.entry_price) / open_trade.entry_price * 100
                else:
                    pct_change = (open_trade.entry_price - exit_price) / open_trade.entry_price * 100

                open_trade.exit_time = times[i]
                open_trade.exit_price = exit_price
                open_trade.pct_change = pct_change
                open_trade.candles_held = i - open_trade_index
                open_trade.exit_reason = exit_reason
                trades.append(open_trade)
                open_trade = None
                open_trade_index = None

            continue

        if current_trend != required_trend:
            continue

        recent_candles = [
            Candle(open=opens[j], high=highs[j], low=lows[j], close=closes[j])
            for j in range(i - needed + 1, i + 1)
        ]

        if detect_pattern(pattern_name, recent_candles):
            fill_price = entry_fill_price(closes[i], pattern_direction)

            if pattern_direction == "BUY":
                stop_loss_price = fill_price * (1 - STOP_LOSS_PCT / 100)
                take_profit_price = fill_price * (1 + TAKE_PROFIT_PCT / 100)
            else:
                stop_loss_price = fill_price * (1 + STOP_LOSS_PCT / 100)
                take_profit_price = fill_price * (1 - TAKE_PROFIT_PCT / 100)

            open_trade = Trade(
                direction=pattern_direction,
                entry_time=times[i],
                entry_price=fill_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
            )
            open_trade_index = i

    return trades


def apply_commission(trades: List[Trade], commission_rate_pct: float) -> List[Trade]:
    adjusted = []
    for t in trades:
        if t.pct_change is None:
            continue
        new_t = Trade(**{**t.__dict__, "pct_change": t.pct_change - commission_rate_pct})
        adjusted.append(new_t)
    return adjusted


def run_for_timeframe(timeframe: str) -> None:
    print(f"\n{'='*90}")
    print(f"TIMEFRAME: {timeframe}")
    print(f"{'='*90}")

    df = load_candles(timeframe)
    print(f"Candles loaded: {len(df):,} (range: {df.index[0]} to {df.index[-1]})\n")

    candles_1h_path = os.path.join(DATA_DIR, f"{SYMBOL}_1h.csv")
    if os.path.exists(candles_1h_path):
        candles_1h = load_candles("1h")
    else:
        candles_1h = (
            df.resample("1h")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
    trend = compute_ema50_1h_trend(candles_1h)

    print(
        f"{'Pattern':<24} | {'Trades':>7} | {'Win Rate':>9} | {'Return':>10} | "
        f"{'Avg Win':>9} | {'Avg Loss':>9}"
    )
    print("-" * 90)

    for pattern_name in PATTERN_DEFS:
        trades = simulate_pattern(df, trend, pattern_name)
        decided = [t for t in trades if t.pct_change is not None]

        if not decided:
            print(f"{pattern_name:<24} | {'(no trades)':>7} |")
            continue

        trades_c = apply_commission(decided, COMMISSION_RATE_PCT)
        wins = [t for t in trades_c if t.pct_change > 0]
        losses = [t for t in trades_c if t.pct_change <= 0]

        total_return = sum(t.pct_change for t in trades_c)
        win_rate = len(wins) / len(trades_c) * 100
        avg_win = sum(t.pct_change for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pct_change for t in losses) / len(losses) if losses else 0

        print(
            f"{pattern_name:<24} | {len(trades_c):>7} | {win_rate:>8.1f}% | "
            f"{total_return:>+9.3f}% | {avg_win:>+8.4f}% | {avg_loss:>+8.4f}%"
        )


def main() -> None:
    print(f"Testing {len(PATTERN_DEFS)} candlestick patterns, each paired with our")
    print(
        f"validated EMA(50)/1h trend filter "
        f"(SL={STOP_LOSS_PCT}%, TP={TAKE_PROFIT_PCT}%, RR=1:{RISK_REWARD_RATIO}).\n"
    )

    for timeframe in ["1min", "5min"]:
        try:
            run_for_timeframe(timeframe)
        except FileNotFoundError as exc:
            print(f"\nSkipping {timeframe}: {exc}")


if __name__ == "__main__":
    main()
