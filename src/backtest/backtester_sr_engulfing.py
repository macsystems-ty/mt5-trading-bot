"""
backtester_sr_engulfing.py

Tests the user's strategy:
1. Identify SUPPORT/RESISTANCE levels as swing highs/lows -- points
   where price was REJECTED (a local high with lower highs on both
   sides = resistance; a local low with higher lows on both sides =
   support).
2. Watch for price to RETEST a previously identified level (price
   trades back within a small tolerance of that level).
3. On the retest, require an ENGULFING candle (body of current candle
   fully covers body of the previous candle) in the direction away
   from the level:
     - At SUPPORT: bullish engulfing (green engulfs red) -> BUY
     - At RESISTANCE: bearish engulfing (red engulfs green) -> SELL
4. Exit via the same candle-based trailing stop used elsewhere.

Tested in TWO modes:
   a) WITHOUT the EMA-1h trend filter (pure S/R + engulfing)
   b) WITH the EMA-1h trend filter (engulfing must also agree with
      the broader trend direction)

Run with:
    python src/backtest/backtester_sr_engulfing.py
"""

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402

SWING_LOOKBACK = 5
RETEST_TOLERANCE_PCT = 0.02
TRAILING_WINDOW = 4
LEVEL_EXPIRY_CANDLES = 500


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    level_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None


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

    def summary(self) -> str:
        lines = [
            "=" * 60,
            f"BACKTEST RESULTS -- {self.label}",
            "=" * 60,
            f"Total trades:   {self.total_trades}",
            f"Win rate:       {self.win_rate:.1f}%",
            f"Avg candles held: {self.avg_candles_held:.1f}",
            f"Total % return: {self.total_pct_return:+.3f}% (net of spread)",
            "=" * 60,
        ]
        return "\n".join(lines)


def identify_swing_levels(df: pd.DataFrame, lookback: int) -> List[dict]:
    highs = df["high"].values
    lows = df["low"].values
    levels = []

    for i in range(lookback, len(df) - lookback):
        window_highs = highs[i - lookback : i + lookback + 1]
        window_lows = lows[i - lookback : i + lookback + 1]

        if highs[i] == max(window_highs):
            levels.append({"index": i, "price": highs[i], "type": "resistance"})

        if lows[i] == min(window_lows):
            levels.append({"index": i, "price": lows[i], "type": "support"})

    return levels


def is_bullish_engulfing(prev_open, prev_close, cur_open, cur_close) -> bool:
    prev_is_red = prev_close < prev_open
    cur_is_green = cur_close > cur_open
    if not (prev_is_red and cur_is_green):
        return False
    return cur_open <= prev_close and cur_close >= prev_open


def is_bearish_engulfing(prev_open, prev_close, cur_open, cur_close) -> bool:
    prev_is_green = prev_close > prev_open
    cur_is_red = cur_close < cur_open
    if not (prev_is_green and cur_is_red):
        return False
    return cur_open >= prev_close and cur_close <= prev_open


def entry_fill_price(mid_price: float, direction: str) -> float:
    return mid_price * (
        1 + bt.HALF_SPREAD_PCT if direction == "BUY" else 1 - bt.HALF_SPREAD_PCT
    )


def exit_fill_price(mid_price: float, direction: str) -> float:
    return mid_price * (
        1 - bt.HALF_SPREAD_PCT if direction == "BUY" else 1 + bt.HALF_SPREAD_PCT
    )


def simulate_trades(
    df: pd.DataFrame,
    trend_series: Optional[pd.Series],
    trailing_window: int,
) -> BacktestResult:
    label = "S/R + Engulfing (no trend filter)" if trend_series is None else "S/R + Engulfing + EMA trend filter"
    result = BacktestResult(label=label)

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = (
        trend_series.reindex(times, method="ffill") if trend_series is not None else None
    )

    all_levels = identify_swing_levels(df, SWING_LOOKBACK)
    level_becomes_known_at = {
        lvl["index"] + SWING_LOOKBACK: lvl for lvl in all_levels
    }

    loop_start = SWING_LOOKBACK * 2 + 2

    # Pre-populate any levels that became known BEFORE the loop starts --
    # otherwise they'd be silently skipped, since the loop only checks
    # level_becomes_known_at for indices it actually iterates over.
    active_levels: List[dict] = [
        lvl for known_at, lvl in level_becomes_known_at.items() if known_at < loop_start
    ]

    open_trade: Optional[Trade] = None
    open_trade_index: Optional[int] = None
    trailing_stop: Optional[float] = None

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_high = highs[i]
        current_low = lows[i]
        current_close = closes[i]

        if i in level_becomes_known_at:
            active_levels.append(level_becomes_known_at[i])
        active_levels = [
            lvl for lvl in active_levels if i - lvl["index"] <= LEVEL_EXPIRY_CANDLES
        ]

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
                    pct_change = (exit_price - entry_price) / entry_price * 100
                else:
                    pct_change = (entry_price - exit_price) / entry_price * 100

                open_trade.exit_time = current_time
                open_trade.exit_price = exit_price
                open_trade.pct_change = pct_change
                open_trade.candles_held = candles_held

                result.trades.append(open_trade)
                open_trade = None
                open_trade_index = None
                trailing_stop = None

            continue

        trend = trend_aligned.iloc[i] if trend_aligned is not None else None

        for level in active_levels:
            level_price = level["price"]
            tolerance = level_price * (RETEST_TOLERANCE_PCT / 100)

            # The RETEST is the approach toward the level -- this is the
            # candle being engulfed (i-1), not the engulfing candle
            # itself (i), which by definition closes AWAY from the level.
            # Use that candle's low (for support) / high (for resistance)
            # to check genuine proximity to the level.
            if level["type"] == "support":
                retest_price = lows[i - 1]
            else:
                retest_price = highs[i - 1]

            price_near_level = (
                level_price - tolerance <= retest_price <= level_price + tolerance
            )
            if not price_near_level:
                continue

            if level["type"] == "support":
                engulfing = is_bullish_engulfing(
                    opens[i - 1], closes[i - 1], opens[i], closes[i]
                )
                direction = "BUY"
            else:
                engulfing = is_bearish_engulfing(
                    opens[i - 1], closes[i - 1], opens[i], closes[i]
                )
                direction = "SELL"

            if not engulfing:
                continue

            if trend_aligned is not None:
                if direction == "BUY" and trend != "UP":
                    continue
                if direction == "SELL" and trend != "DOWN":
                    continue

            fill_price = entry_fill_price(current_close, direction)
            open_trade = Trade(
                direction=direction,
                entry_time=current_time,
                entry_price=fill_price,
                level_price=level_price,
            )
            open_trade_index = i

            window_start = max(0, i - trailing_window + 1)
            trailing_stop = (
                min(lows[window_start : i + 1])
                if direction == "BUY"
                else max(highs[window_start : i + 1])
            )
            break

    return result


def main() -> None:
    print(f"Loading candles for {bt.SYMBOL} ...")
    candles_1min = bt.load_candles("1min")
    print(f"  1min candles: {len(candles_1min)}")

    print(
        f"\nIdentifying swing-based support/resistance levels "
        f"(lookback={SWING_LOOKBACK} candles each side) ..."
    )
    levels = identify_swing_levels(candles_1min, SWING_LOOKBACK)
    support_count = sum(1 for l in levels if l["type"] == "support")
    resistance_count = sum(1 for l in levels if l["type"] == "resistance")
    print(f"  Found {support_count} support levels, {resistance_count} resistance levels")

    print("\n" + "#" * 60)
    print("VARIANT 1: S/R + Engulfing, NO trend filter")
    print("#" * 60)
    result_no_trend = simulate_trades(candles_1min, None, TRAILING_WINDOW)
    print(result_no_trend.summary())

    print("\n" + "#" * 60)
    print("VARIANT 2: S/R + Engulfing, WITH EMA-1h trend filter")
    print("#" * 60)
    candles_1h_path = os.path.join(bt.DATA_DIR, f"{bt.SYMBOL}_1h.csv")
    if os.path.exists(candles_1h_path):
        candles_1h = bt.load_candles("1h")
    else:
        candles_1h = (
            candles_1min.resample("1h")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
    trend_series = bt.compute_ema_1h_trend(candles_1h)
    result_with_trend = simulate_trades(candles_1min, trend_series, TRAILING_WINDOW)
    print(result_with_trend.summary())

    print("\n" + "=" * 60)
    print("COMPARISON")
    print("=" * 60)
    print(
        f"No trend filter:   {result_no_trend.total_trades} trades, "
        f"{result_no_trend.win_rate:.1f}% win rate, {result_no_trend.total_pct_return:+.3f}% return"
    )
    print(
        f"With trend filter: {result_with_trend.total_trades} trades, "
        f"{result_with_trend.win_rate:.1f}% win rate, {result_with_trend.total_pct_return:+.3f}% return"
    )
    print(
        f"\nFor reference, our existing validated baseline "
        f"(EMA trend + pullback=2 + trail=4): +3.073% (570 trades, 30.7% win rate)"
    )

    if result_no_trend.total_trades > 0:
        print(f"\nFirst 10 trades (no trend filter):")
        for t in result_no_trend.trades[:10]:
            print(
                f"  {t.entry_time} {t.direction:>4} @ {t.entry_price:.2f} "
                f"(level: {t.level_price:.2f}) -> {t.exit_time} @ {t.exit_price:.2f} | "
                f"held {t.candles_held} candles ({t.pct_change:+.3f}%)"
            )


if __name__ == "__main__":
    main()
