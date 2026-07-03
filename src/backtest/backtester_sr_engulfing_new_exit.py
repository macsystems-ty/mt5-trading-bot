"""
backtester_sr_engulfing_new_exit.py

Reuses our VALIDATED S/R + Engulfing + EMA(1H) trend entry logic
EXACTLY (same swing-level detection, same retest tolerance, same
engulfing pattern check, same trend-agreement gate), but replaces the
EXIT logic with a new three-stage mechanism per the user's
specification:

  STAGE 1 (immediately after entry):
    Initial stop-loss = the most extreme point (low for BUY, high for
    SELL) among the ENTRY candle and the ONE candle immediately before
    it (i.e. 2 candles total: entry candle + 1 prior).

  STAGE 2 (after exactly 1 favorable candle closes):
    Stop-loss moves to BREAKEVEN (the entry price).

  STAGE 3 (after exactly 2 favorable candles have closed since entry):
    The stop-loss becomes a true "trailing" mechanism: from this point
    on, the very NEXT candle that closes AGAINST the trade direction
    closes the trade immediately.

"Favorable candle" = a candle that closes in the trade's direction
(green/bullish for a BUY, red/bearish for a SELL).

Run with:
    python src/backtest/backtester_sr_engulfing_new_exit.py
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
import backtester_sr_engulfing as sre  # noqa: E402

SWING_LOOKBACK = 5
RETEST_TOLERANCE_PCT = 0.01


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    level_price: float
    initial_stop_distance_pct: Optional[float] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    candles_held: Optional[int] = None
    pct_change: Optional[float] = None
    exit_reason: Optional[str] = None


@dataclass
class Result:
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

    def line(self) -> str:
        return (
            f"{self.label:<48} | trades={self.total_trades:>4} | "
            f"win_rate={self.win_rate:>5.1f}% | return={self.total_pct_return:>+8.3f}%"
        )


def is_favorable_candle(open_price: float, close_price: float, direction: str) -> bool:
    if direction == "BUY":
        return close_price > open_price
    else:
        return close_price < open_price


def simulate(df: pd.DataFrame, trend_series: pd.Series, label: str) -> Result:
    result = Result(label=label)

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    times = df.index

    trend_aligned = trend_series.reindex(times, method="ffill")

    all_levels = sre.identify_swing_levels(df, SWING_LOOKBACK)
    level_becomes_known_at = {
        lvl["index"] + SWING_LOOKBACK: lvl for lvl in all_levels
    }
    loop_start = SWING_LOOKBACK * 2 + 2

    active_levels: List[dict] = [
        dict(lvl, tested_count=0)
        for known_at, lvl in level_becomes_known_at.items()
        if known_at < loop_start
    ]

    open_trade: Optional[Trade] = None
    open_trade_index: Optional[int] = None
    stop_loss_price: Optional[float] = None
    favorable_candles_since_entry: int = 0
    trailing_active: bool = False

    for i in range(loop_start, len(df)):
        current_time = times[i]
        current_open = opens[i]
        current_high = highs[i]
        current_low = lows[i]
        current_close = closes[i]
        trend = trend_aligned.iloc[i]

        if i in level_becomes_known_at:
            active_levels.append(dict(level_becomes_known_at[i], tested_count=0))
        active_levels = [
            lvl for lvl in active_levels if i - lvl["index"] <= 500
        ]

        if open_trade is not None:
            candles_held = i - open_trade_index
            should_close = False
            exit_level = None
            exit_reason = None

            if trailing_active:
                favorable = is_favorable_candle(current_open, current_close, open_trade.direction)
                if not favorable:
                    should_close = True
                    exit_level = current_close
                    exit_reason = "stage3_retracement_candle"
            else:
                if open_trade.direction == "BUY":
                    hit_stop = current_low <= stop_loss_price
                else:
                    hit_stop = current_high >= stop_loss_price

                if hit_stop:
                    should_close = True
                    exit_level = stop_loss_price
                    exit_reason = "stage1_2_stop_hit"

                favorable = is_favorable_candle(current_open, current_close, open_trade.direction)
                if favorable:
                    favorable_candles_since_entry += 1

                    if favorable_candles_since_entry == 1:
                        stop_loss_price = open_trade.entry_price
                    elif favorable_candles_since_entry >= 2:
                        trailing_active = True

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

                result.trades.append(open_trade)
                open_trade = None
                open_trade_index = None
                stop_loss_price = None
                favorable_candles_since_entry = 0
                trailing_active = False

            continue

        for level in active_levels:
            level_price = level["price"]
            tolerance = level_price * (RETEST_TOLERANCE_PCT / 100)

            if level["type"] == "support":
                retest_price = lows[i - 1]
            else:
                retest_price = highs[i - 1]

            price_near_level = (
                level_price - tolerance <= retest_price <= level_price + tolerance
            )
            if not price_near_level:
                continue

            level["tested_count"] += 1

            if level["type"] == "support":
                engulfing = sre.is_bullish_engulfing(
                    opens[i - 1], closes[i - 1], opens[i], closes[i]
                )
                direction = "BUY"
            else:
                engulfing = sre.is_bearish_engulfing(
                    opens[i - 1], closes[i - 1], opens[i], closes[i]
                )
                direction = "SELL"

            if not engulfing:
                continue

            if direction == "BUY" and trend != "UP":
                continue
            if direction == "SELL" and trend != "DOWN":
                continue

            fill_price = sre.entry_fill_price(current_close, direction)

            if direction == "BUY":
                initial_stop = min(lows[i - 1], lows[i])
            else:
                initial_stop = max(highs[i - 1], highs[i])

            stop_distance_pct = abs(fill_price - initial_stop) / fill_price * 100

            open_trade = Trade(
                direction=direction,
                entry_time=current_time,
                entry_price=fill_price,
                level_price=level_price,
                initial_stop_distance_pct=stop_distance_pct,
            )
            open_trade_index = i
            stop_loss_price = initial_stop
            favorable_candles_since_entry = 0
            trailing_active = False

            break

    return result


def apply_commission(trades: List[Trade], commission_rate_pct: float) -> List[Trade]:
    adjusted = []
    for t in trades:
        if t.pct_change is None:
            continue
        new_t = Trade(**{**t.__dict__, "pct_change": t.pct_change - commission_rate_pct})
        adjusted.append(new_t)
    return adjusted


def build_trend_series_for_range(entry_candles: pd.DataFrame) -> pd.Series:
    candles_1h = (
        entry_candles.resample("1h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    return bt.compute_ema_1h_trend(candles_1h)


def main() -> None:
    COMMISSION_RATE_PCT = 0.02 / 160 * 100

    print(f"Loading 5min candles for {bt.SYMBOL} (our best validated timeframe) ...")
    candles_5min = bt.load_candles("5min")
    print(f"  Candles: {len(candles_5min):,} (range: {candles_5min.index[0]} to {candles_5min.index[-1]})\n")

    trend_series = build_trend_series_for_range(candles_5min)

    print("Running NEW 3-stage exit (initial SL -> breakeven -> strict 1-candle trailing) ...\n")
    result = simulate(candles_5min, trend_series, label="new_exit_5min")

    decided = [t for t in result.trades if t.pct_change is not None]
    trades_c = apply_commission(decided, COMMISSION_RATE_PCT)

    wins = [t for t in trades_c if t.pct_change > 0]
    losses = [t for t in trades_c if t.pct_change <= 0]
    total_return = sum(t.pct_change for t in trades_c)
    win_rate = len(wins) / len(trades_c) * 100 if trades_c else 0

    print("=" * 70)
    print("RESULTS (new 3-stage exit, real spread + commission included)")
    print("=" * 70)
    print(f"Total trades: {len(trades_c)}")
    print(f"Win rate:     {win_rate:.1f}%")
    print(f"Total return: {total_return:+.3f}%")
    print("=" * 70)

    exit_reason_counts = {}
    for t in trades_c:
        exit_reason_counts[t.exit_reason] = exit_reason_counts.get(t.exit_reason, 0) + 1
    print("\nExit reason breakdown:")
    for reason, count in exit_reason_counts.items():
        print(f"  {reason}: {count}")

    print("\nFirst 15 trades:")
    for t in trades_c[:15]:
        print(
            f"  {t.entry_time} {t.direction} @ {t.entry_price:.2f} -> "
            f"{t.exit_time} @ {t.exit_price:.2f} ({t.pct_change:+.4f}%) [{t.exit_reason}]"
        )

    print(
        "\nCOMPARE against our previously validated result (simple trailing\n"
        "stop, window=3): 1,084 trades, 40.9% win rate, +14.429% return."
    )


if __name__ == "__main__":
    main()
