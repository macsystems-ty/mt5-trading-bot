"""
candlestick_patterns.py

Detects classic candlestick patterns from OHLC candles. Each function
takes the relevant candles and returns True/False.

Patterns implemented:
  Reversal: Bullish Engulfing, Bearish Engulfing, Hammer, Inverted
            Hammer, Shooting Star, Morning Star, Evening Star, Doji,
            Piercing Line, Dark Cloud Cover
  Continuation: Three White Soldiers, Three Black Crows,
            Rising Three Methods, Falling Three Methods

All functions are pure (no side effects) and designed to be unit
tested individually before being wired into any backtester.
"""

from dataclasses import dataclass


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


def is_doji(c: Candle, body_to_range_max: float = 0.1) -> bool:
    if c.range == 0:
        return False
    return (c.body / c.range) <= body_to_range_max


def is_hammer(c: Candle, lower_wick_min_ratio: float = 2.0, upper_wick_max_ratio: float = 0.3) -> bool:
    if c.body == 0:
        return False
    return (
        c.lower_wick >= c.body * lower_wick_min_ratio
        and c.upper_wick <= c.body * upper_wick_max_ratio
    )


def is_inverted_hammer(c: Candle, upper_wick_min_ratio: float = 2.0, lower_wick_max_ratio: float = 0.3) -> bool:
    if c.body == 0:
        return False
    return (
        c.upper_wick >= c.body * upper_wick_min_ratio
        and c.lower_wick <= c.body * lower_wick_max_ratio
    )


def is_shooting_star(c: Candle, upper_wick_min_ratio: float = 2.0, lower_wick_max_ratio: float = 0.3) -> bool:
    return is_inverted_hammer(c, upper_wick_min_ratio, lower_wick_max_ratio)


def is_bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    return (
        prev.is_bearish
        and cur.is_bullish
        and cur.open <= prev.close
        and cur.close >= prev.open
    )


def is_bearish_engulfing(prev: Candle, cur: Candle) -> bool:
    return (
        prev.is_bullish
        and cur.is_bearish
        and cur.open >= prev.close
        and cur.close <= prev.open
    )


def is_piercing_line(prev: Candle, cur: Candle) -> bool:
    if not (prev.is_bearish and cur.is_bullish):
        return False
    prev_midpoint = (prev.open + prev.close) / 2
    return cur.open < prev.close and cur.close > prev_midpoint and cur.close < prev.open


def is_dark_cloud_cover(prev: Candle, cur: Candle) -> bool:
    if not (prev.is_bullish and cur.is_bearish):
        return False
    prev_midpoint = (prev.open + prev.close) / 2
    return cur.open > prev.close and cur.close < prev_midpoint and cur.close > prev.open


def is_morning_star(c1: Candle, c2: Candle, c3: Candle, small_body_max_ratio: float = 0.3) -> bool:
    if not c1.is_bearish:
        return False
    if c1.body == 0:
        return False
    if c2.body > c1.body * small_body_max_ratio:
        return False
    if not c3.is_bullish:
        return False
    c1_midpoint = (c1.open + c1.close) / 2
    return max(c2.open, c2.close) < c1.close and c3.close > c1_midpoint


def is_evening_star(c1: Candle, c2: Candle, c3: Candle, small_body_max_ratio: float = 0.3) -> bool:
    if not c1.is_bullish:
        return False
    if c1.body == 0:
        return False
    if c2.body > c1.body * small_body_max_ratio:
        return False
    if not c3.is_bearish:
        return False
    c1_midpoint = (c1.open + c1.close) / 2
    return min(c2.open, c2.close) > c1.close and c3.close < c1_midpoint


def is_three_white_soldiers(c1: Candle, c2: Candle, c3: Candle) -> bool:
    if not (c1.is_bullish and c2.is_bullish and c3.is_bullish):
        return False
    return (
        c2.close > c1.close
        and c3.close > c2.close
        and c2.open > c1.open and c2.open < c1.close
        and c3.open > c2.open and c3.open < c2.close
    )


def is_three_black_crows(c1: Candle, c2: Candle, c3: Candle) -> bool:
    if not (c1.is_bearish and c2.is_bearish and c3.is_bearish):
        return False
    return (
        c2.close < c1.close
        and c3.close < c2.close
        and c2.open < c1.open and c2.open > c1.close
        and c3.open < c2.open and c3.open > c2.close
    )


def is_rising_three_methods(candles: list) -> bool:
    if len(candles) != 5:
        return False
    c1, c2, c3, c4, c5 = candles
    if not c1.is_bullish or c1.body == 0:
        return False
    middle_within_range = all(
        c1.low <= min(c.open, c.close) and max(c.open, c.close) <= c1.high
        for c in [c2, c3, c4]
    )
    if not middle_within_range:
        return False
    if not c5.is_bullish:
        return False
    return c5.close > c1.close


def is_falling_three_methods(candles: list) -> bool:
    if len(candles) != 5:
        return False
    c1, c2, c3, c4, c5 = candles
    if not c1.is_bearish or c1.body == 0:
        return False
    middle_within_range = all(
        c1.low <= min(c.open, c.close) and max(c.open, c.close) <= c1.high
        for c in [c2, c3, c4]
    )
    if not middle_within_range:
        return False
    if not c5.is_bearish:
        return False
    return c5.close < c1.close
