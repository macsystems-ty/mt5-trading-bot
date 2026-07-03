"""
backtester_ema_fib.py

PHASE 1 (no Martingale yet): Tests a new strategy on 1min candles:
  - Trend: EMA(5) vs EMA(13) on 1min candles (continuous state: UP/DOWN),
    plus separate just_crossed_up/just_crossed_down booleans for the exact
    crossover moment.
  - Entry: identify a recent IMPULSIVE swing (using a stricter swing
    detector than our other strategies -- lookback=20 PLUS a minimum
    0.05% move-size filter, since raw lookback=5/20 swings on 1min data
    are mostly noise, not real legs worth drawing Fibonacci on).
    Compute Fibonacci retracement levels (38.2%, 50%, 61.8%) on that
    swing. When price pulls back into that zone and the current candle
    resumes in the trend direction, enter.
  - Exit: tight trailing stop (lowest low / highest high of last
    TRAILING_WINDOW candles).

This script deliberately does NOT implement Martingale yet -- we need
real win/loss size data from this run before we can compute a
mathematically correct Martingale multiplier (see Phase 2 script).

Run with:
    python src/backtest/backtester_ema_fib.py
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOL = "1HZ25V"

EMA_FAST = 5
EMA_SLOW = 13

SWING_LOOKBACK = 20
MIN_SWING_MOVE_PCT = 0.05

FIB_ZONE_MIN = 0.382
FIB_ZONE_MAX = 0.618

import os as _os
TRAILING_WINDOW = int(_os.getenv("EMA_FIB_TRAILING_WINDOW", "3"))

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


def compute_ema_trend(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = out["close"].ewm(span=EMA_FAST, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    out["trend"] = "DOWN"
    out.loc[out["ema_fast"] > out["ema_slow"], "trend"] = "UP"

    prev_trend = out["trend"].shift(1)
    out["just_crossed_up"] = (out["trend"] == "UP") & (prev_trend == "DOWN")
    out["just_crossed_down"] = (out["trend"] == "DOWN") & (prev_trend == "UP")

    return out


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str


def detect_filtered_swings(df: pd.DataFrame) -> List[SwingPoint]:
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    raw_swings: List[SwingPoint] = []
    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        window_highs = highs[i - SWING_LOOKBACK : i + SWING_LOOKBACK + 1]
        window_lows = lows[i - SWING_LOOKBACK : i + SWING_LOOKBACK + 1]
        if highs[i] == window_highs.max():
            raw_swings.append(SwingPoint(index=i, price=highs[i], kind="high"))
        if lows[i] == window_lows.min():
            raw_swings.append(SwingPoint(index=i, price=lows[i], kind="low"))

    raw_swings.sort(key=lambda s: s.index)

    if not raw_swings:
        return []

    # CRITICAL: a valid Fibonacci leg requires ALTERNATING high/low swings
    # (a leg runs from a swing high to a swing low, or vice versa -- never
    # between two swings of the same type). The filter below enforces this:
    #   - If the new swing is the SAME type as the last kept swing, only
    #     replace it if the new one is MORE extreme (a better high/low for
    #     the same leg still forming).
    #   - If the new swing is the OPPOSITE type, only keep it as the next
    #     leg point if it represents at least MIN_SWING_MOVE_PCT move from
    #     the last kept swing (a real reversal, not noise).
    filtered = [raw_swings[0]]
    for swing in raw_swings[1:]:
        last = filtered[-1]

        if swing.kind == last.kind:
            is_more_extreme = (
                swing.price > last.price if swing.kind == "high" else swing.price < last.price
            )
            if is_more_extreme:
                filtered[-1] = swing
            continue

        move_pct = abs(swing.price - last.price) / last.price * 100
        if move_pct >= MIN_SWING_MOVE_PCT:
            filtered.append(swing)

    return filtered


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    pct_change: Optional[float] = None
    candles_held: Optional[int] = None


def simulate(df: pd.DataFrame) -> List[Trade]:
    df = compute_ema_trend(df)
    filtered_swings = detect_filtered_swings(df)

    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    trends = df["trend"].values
    times = df.index

    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    open_trade_index: Optional[int] = None
    trailing_stop: Optional[float] = None

    swing_becomes_known_at = {s.index + SWING_LOOKBACK: s for s in filtered_swings}
    known_swings: List[SwingPoint] = []

    start = SWING_LOOKBACK * 2 + 2

    for i in range(start):
        if i in swing_becomes_known_at:
            known_swings.append(swing_becomes_known_at[i])

    for i in range(start, len(df)):
        if i in swing_becomes_known_at:
            known_swings.append(swing_becomes_known_at[i])
        known_swings_recent = known_swings[-50:]

        current_close = closes[i]
        current_open = opens[i]
        current_high = highs[i]
        current_low = lows[i]
        current_trend = trends[i]

        if open_trade is not None:
            window_start = max(0, i - TRAILING_WINDOW)
            if open_trade.direction == "BUY":
                new_stop = min(lows[window_start:i])
                if trailing_stop is None or new_stop > trailing_stop:
                    trailing_stop = new_stop
                should_close = current_low <= trailing_stop
            else:
                new_stop = max(highs[window_start:i])
                if trailing_stop is None or new_stop < trailing_stop:
                    trailing_stop = new_stop
                should_close = current_high >= trailing_stop

            if should_close:
                exit_price = exit_fill_price(trailing_stop, open_trade.direction)
                if open_trade.direction == "BUY":
                    pct_change = (exit_price - open_trade.entry_price) / open_trade.entry_price * 100
                else:
                    pct_change = (open_trade.entry_price - exit_price) / open_trade.entry_price * 100

                open_trade.exit_time = times[i]
                open_trade.exit_price = exit_price
                open_trade.pct_change = pct_change
                open_trade.candles_held = i - open_trade_index
                trades.append(open_trade)
                open_trade = None
                trailing_stop = None
                open_trade_index = None

            continue

        if len(known_swings_recent) < 3:
            continue

        # The IMPULSIVE leg (the one we draw Fibonacci on) runs from the
        # swing BEFORE the pullback to the swing AT the start of the
        # pullback. The most recent swing (known_swings_recent[-1]) is
        # the pullback's own extreme point -- NOT part of the impulsive
        # leg itself. Using the most recent leg directly as "the leg"
        # was the bug: that leg IS the pullback, which naturally points
        # against the trend, so it could never satisfy a same-direction
        # check against the trend.
        impulsive_start = known_swings_recent[-3]
        impulsive_end = known_swings_recent[-2]
        pullback_extreme = known_swings_recent[-1]

        leg_start_price = impulsive_start.price
        leg_end_price = impulsive_end.price

        if leg_end_price == leg_start_price:
            continue

        leg_range = leg_end_price - leg_start_price
        fib_zone_a = leg_end_price - leg_range * FIB_ZONE_MIN
        fib_zone_b = leg_end_price - leg_range * FIB_ZONE_MAX
        fib_zone_low = min(fib_zone_a, fib_zone_b)
        fib_zone_high = max(fib_zone_a, fib_zone_b)

        # Confirm the pullback's extreme actually reached into the fib zone
        # (this is what makes the pullback "deep enough" to be a valid
        # retracement, not just a shallow wiggle).
        pullback_reached_zone = fib_zone_low <= pullback_extreme.price <= fib_zone_high

        prev_close = closes[i - 1]
        prev_open = opens[i - 1]

        if (
            current_trend == "UP"
            and leg_end_price > leg_start_price  # impulsive leg points UP
            and pullback_extreme.kind == "low"   # pullback was a dip
            and pullback_reached_zone
        ):
            resumes_up = current_close > current_open and current_close > prev_close
            if resumes_up:
                fill_price = entry_fill_price(current_close, "BUY")
                open_trade = Trade(direction="BUY", entry_time=times[i], entry_price=fill_price)
                open_trade_index = i
                trailing_stop = min(lows[max(0, i - TRAILING_WINDOW + 1) : i + 1])
                continue

        if (
            current_trend == "DOWN"
            and leg_end_price < leg_start_price  # impulsive leg points DOWN
            and pullback_extreme.kind == "high"  # pullback was a bounce
            and pullback_reached_zone
        ):
            resumes_down = current_close < current_open and current_close < prev_close
            if resumes_down:
                fill_price = entry_fill_price(current_close, "SELL")
                open_trade = Trade(direction="SELL", entry_time=times[i], entry_price=fill_price)
                open_trade_index = i
                trailing_stop = max(highs[max(0, i - TRAILING_WINDOW + 1) : i + 1])
                continue

    return trades


def apply_commission(trades: List[Trade], commission_rate_pct: float) -> List[Trade]:
    adjusted = []
    for t in trades:
        if t.pct_change is None:
            continue
        new_t = Trade(**{**t.__dict__, "pct_change": t.pct_change - commission_rate_pct})
        adjusted.append(new_t)
    return adjusted


def main() -> None:
    print(f"Loading 1min candles for {SYMBOL} ...")
    df = load_candles("1min")
    print(f"  Candles: {len(df):,}")

    print(
        f"\nDetecting Fibonacci-grade swings (lookback={SWING_LOOKBACK}, "
        f"min_move={MIN_SWING_MOVE_PCT}%) ..."
    )
    swings = detect_filtered_swings(df)
    print(f"  Found {len(swings)} qualifying swings (out of far more raw swing points)")

    print(f"\nRunning EMA{EMA_FAST}/EMA{EMA_SLOW} trend + Fibonacci pullback strategy ...")
    print(f"Fib zone: {FIB_ZONE_MIN*100:.1f}% to {FIB_ZONE_MAX*100:.1f}% retracement")
    print(f"Trailing stop window: {TRAILING_WINDOW} candles\n")

    trades = simulate(df)
    decided = [t for t in trades if t.pct_change is not None]

    if not decided:
        print("No trades generated. Conditions may be too strict for this dataset.")
        return

    trades_with_commission = apply_commission(decided, COMMISSION_RATE_PCT)

    wins = [t for t in trades_with_commission if t.pct_change > 0]
    losses = [t for t in trades_with_commission if t.pct_change <= 0]

    total_return = sum(t.pct_change for t in trades_with_commission)
    win_rate = len(wins) / len(trades_with_commission) * 100

    avg_win = sum(t.pct_change for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pct_change for t in losses) / len(losses) if losses else 0

    print("=" * 70)
    print("PHASE 1 RESULTS (no Martingale, real spread + commission included)")
    print("=" * 70)
    print(f"Total trades:     {len(trades_with_commission)}")
    print(f"Win rate:         {win_rate:.1f}%")
    print(f"Total return:     {total_return:+.3f}%")
    print(f"Avg win size:     {avg_win:+.4f}%")
    print(f"Avg loss size:    {avg_loss:+.4f}%")
    print(f"Win/Loss size ratio (|avg_win/avg_loss|): {abs(avg_win/avg_loss) if avg_loss else 0:.3f}")

    win_holds = [t.candles_held for t in wins if t.candles_held is not None]
    loss_holds = [t.candles_held for t in losses if t.candles_held is not None]
    avg_win_hold = sum(win_holds) / len(win_holds) if win_holds else 0
    avg_loss_hold = sum(loss_holds) / len(loss_holds) if loss_holds else 0
    print(f"Avg candles held (WINS):  {avg_win_hold:.1f}")
    print(f"Avg candles held (LOSSES): {avg_loss_hold:.1f}")
    print("=" * 70)

    print("\nFirst 15 trades:")
    for t in trades_with_commission[:15]:
        print(
            f"  {t.entry_time} {t.direction} @ {t.entry_price:.2f} -> "
            f"{t.exit_time} @ {t.exit_price:.2f} ({t.pct_change:+.4f}%)"
        )

    print(
        "\nNOTE: This is Phase 1 (no Martingale). Use the win/loss size "
        "ratio above to compute the mathematically correct Martingale "
        "multiplier in Phase 2 -- do not assume 2x without checking this "
        "ratio first."
    )


if __name__ == "__main__":
    main()