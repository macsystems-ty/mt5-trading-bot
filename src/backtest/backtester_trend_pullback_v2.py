"""
backtester_trend_pullback_v2.py

Same trend + pullback entry logic as backtester_trend_pullback.py, but
with a CONFIGURABLE trailing-stop window: instead of always trailing
behind just the single last candle, this trails behind the lowest low
(for BUY) or highest high (for SELL) of the last N candles, where N is
swept across 1, 2, and 3 for comparison.

Run with:
    python src/backtest/backtester_trend_pullback_v2.py
"""

import os
import sys
from dataclasses import dataclass, field
from itertools import product
from typing import List, Optional

import pandas as pd

STRATEGY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "strategy"
)
sys.path.insert(0, STRATEGY_PATH)

import indicators  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOL = os.getenv("DERIV_SYMBOL", "1HZ25V")

SPREAD_PCT = 58 / 849362
HALF_SPREAD_PCT = SPREAD_PCT / 2

PULLBACK_LENGTHS = [1, 2]
TRAILING_WINDOWS = [1, 2, 3, 4, 5, 6]
TREND_METHODS = ["ema_1h"]  # daily_candle already shown to consistently underperform


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None
    initial_stop_distance_pct: Optional[float] = None  # the % distance from entry to the FIRST stop placement


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    label: str = ""

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pct_change is not None and t.pct_change > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pct_change is not None and t.pct_change <= 0)

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return (self.wins / decided * 100) if decided > 0 else 0.0

    @property
    def total_pct_return(self) -> float:
        return sum(t.pct_change for t in self.trades if t.pct_change is not None)

    @property
    def avg_candles_held(self) -> float:
        held = [t.candles_held for t in self.trades if t.candles_held is not None]
        return sum(held) / len(held) if held else 0.0

    def summary_line(self) -> str:
        return (
            f"{self.label:<32} | trades={self.total_trades:>5} | "
            f"win_rate={self.win_rate:>5.1f}% | avg_hold={self.avg_candles_held:>5.1f} | "
            f"return={self.total_pct_return:>+8.3f}%"
        )


def load_candles(timeframe: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"{SYMBOL}_{timeframe}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing {path}. Run fetch_history.py first.")
    return pd.read_csv(path, index_col="open_time", parse_dates=True)


def candle_color(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "GREEN"
    if close_price < open_price:
        return "RED"
    return "FLAT"


def compute_ema_1h_trend(candles_1h: pd.DataFrame) -> pd.Series:
    """
    Trend filter on 1H candles. Uses EMA(14) -- adopted after
    systematically testing EMA periods 14/20/30/50 on 1h candles
    (compare_trend_filter_speeds.py): EMA14 gave the best result
    (+42.593% vs baseline's +19.936%) with a measured, non-noisy flip
    rate (1,514 flips over ~105k candles, vs the rejected EMA5/13-on-
    1min's ~every-15-minutes flip rate).
    """
    result = indicators.add_all_indicators(candles_1h)
    ema = result["ema_14"]
    close = result["close"]

    ema_rising = ema.diff() > 0
    price_above = close > ema

    trend = pd.Series("FLAT", index=candles_1h.index)
    trend[price_above & ema_rising] = "UP"
    trend[(~price_above) & (~ema_rising)] = "DOWN"
    return trend


def compute_daily_candle_trend(candles_1min: pd.DataFrame) -> pd.Series:
    df = candles_1min.copy()
    df["date"] = df.index.date
    daily_open = df.groupby("date")["open"].transform("first")

    trend = pd.Series("FLAT", index=df.index)
    trend[df["close"] > daily_open] = "UP"
    trend[df["close"] < daily_open] = "DOWN"
    return trend


def entry_fill_price(mid_price: float, direction: str) -> float:
    return mid_price * (1 + HALF_SPREAD_PCT if direction == "BUY" else 1 - HALF_SPREAD_PCT)


def exit_fill_price(mid_price: float, direction: str) -> float:
    return mid_price * (1 - HALF_SPREAD_PCT if direction == "BUY" else 1 + HALF_SPREAD_PCT)


def simulate_trades(
    df: pd.DataFrame,
    trend_series: pd.Series,
    pullback_length: int,
    trailing_window: int,
) -> BacktestResult:
    result = BacktestResult(label=f"pullback={pullback_length}, trail={trailing_window}")

    colors = [candle_color(o, c) for o, c in zip(df["open"], df["close"])]
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")

    open_trade: Optional[Trade] = None
    open_trade_index: Optional[int] = None
    trailing_stop: Optional[float] = None

    start_index = max(pullback_length, trailing_window)

    for i in range(start_index, len(df)):
        current_time = times[i]
        current_high = highs[i]
        current_low = lows[i]
        current_close = closes[i]
        trend = trend_aligned.iloc[i]

        if open_trade is not None:
            candles_held = i - open_trade_index
            window_start = max(0, i - trailing_window)

            if open_trade.direction == "BUY":
                new_stop = min(lows[window_start:i])
                if trailing_stop is None or new_stop > trailing_stop:
                    trailing_stop = new_stop
                stopped_out = current_low <= trailing_stop
            else:
                new_stop = max(highs[window_start:i])
                if trailing_stop is None or new_stop < trailing_stop:
                    trailing_stop = new_stop
                stopped_out = current_high >= trailing_stop

            if stopped_out:
                exit_price = exit_fill_price(trailing_stop, open_trade.direction)
                entry_price = open_trade.entry_price

                if open_trade.direction == "BUY":
                    pct_change = (exit_price - entry_price) / entry_price
                else:
                    pct_change = (entry_price - exit_price) / entry_price

                open_trade.exit_time = current_time
                open_trade.exit_price = exit_price
                open_trade.pct_change = pct_change * 100
                open_trade.candles_held = candles_held

                result.trades.append(open_trade)
                open_trade = None
                open_trade_index = None
                trailing_stop = None

            continue

        if trend == "UP":
            pullback_colors = colors[i - pullback_length : i]
            if all(c == "RED" for c in pullback_colors) and colors[i] == "GREEN":
                direction = "BUY"
            else:
                continue
        elif trend == "DOWN":
            pullback_colors = colors[i - pullback_length : i]
            if all(c == "GREEN" for c in pullback_colors) and colors[i] == "RED":
                direction = "SELL"
            else:
                continue
        else:
            continue

        fill_price = entry_fill_price(current_close, direction)
        open_trade = Trade(direction=direction, entry_time=current_time, entry_price=fill_price)
        open_trade_index = i

        window_start = max(0, i - trailing_window + 1)
        trailing_stop = (
            min(lows[window_start : i + 1])
            if direction == "BUY"
            else max(highs[window_start : i + 1])
        )

        # Record the initial risk distance (entry to stop) as a % of
        # entry price -- this is the REAL per-trade risk unit, used
        # later for correct position sizing (instead of guessing from
        # the average loss across all trades, which mixes early-exit
        # small losses with full-stop-distance losses).
        open_trade.initial_stop_distance_pct = (
            abs(fill_price - trailing_stop) / fill_price * 100
        )

    return result


def main() -> None:
    print(f"Loading candles for {SYMBOL} ...")
    candles_1min = load_candles("1min")
    print(f"  1min candles: {len(candles_1min)}")

    print("\nComputing trend definitions ...")
    candles_1h_path = os.path.join(DATA_DIR, f"{SYMBOL}_1h.csv")
    if os.path.exists(candles_1h_path):
        candles_1h = load_candles("1h")
    else:
        candles_1h = (
            candles_1min.resample("1h")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
        )

    ema_trend = compute_ema_1h_trend(candles_1h)
    daily_trend = compute_daily_candle_trend(candles_1min)

    trend_series_by_method = {
        "ema_1h": ema_trend,
        "daily_candle": daily_trend,
    }

    print(
        f"\nRunning grid: {len(TREND_METHODS)} trend methods x "
        f"{len(PULLBACK_LENGTHS)} pullback lengths x "
        f"{len(TRAILING_WINDOWS)} trailing windows = "
        f"{len(TREND_METHODS) * len(PULLBACK_LENGTHS) * len(TRAILING_WINDOWS)} combinations\n"
    )

    all_results = []
    for trend_method, pullback_length, trailing_window in product(
        TREND_METHODS, PULLBACK_LENGTHS, TRAILING_WINDOWS
    ):
        trend_series = trend_series_by_method[trend_method]
        result = simulate_trades(
            candles_1min, trend_series, pullback_length, trailing_window
        )
        result.label = f"{trend_method} / pullback={pullback_length} / trail={trailing_window}"
        all_results.append(result)

    all_results.sort(key=lambda r: r.total_pct_return, reverse=True)

    print("All combinations, ranked by return:\n")
    for r in all_results:
        print(r.summary_line())

    best = all_results[0]
    print(f"\nBest combination: {best.label}")
    print(f"\nFirst 15 trades for best combination:")
    for t in best.trades[:15]:
        print(
            f"  {t.entry_time} {t.direction:>4} @ {t.entry_price:.2f} -> "
            f"{t.exit_time} @ {t.exit_price:.2f} | "
            f"held {t.candles_held} candles ({t.pct_change:+.3f}%)"
        )


if __name__ == "__main__":
    main()