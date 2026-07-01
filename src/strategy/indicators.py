"""
indicators.py

Technical analysis indicators implemented from scratch using pandas/numpy.
No extra dependencies (e.g. TA-Lib, pandas_ta) required -- keeps the
project lightweight and easy to install on any machine.

All functions take a pandas Series of closing prices (unless noted)
and return a pandas Series or DataFrame aligned to the same index.
"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's smoothing method).

    Returns values from 0-100.
    - Above 70 is generally considered "overbought"
    - Below 30 is generally considered "oversold"
    """
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi_values = 100 - (100 / (1 + rs))

    # When avg_loss is 0, RSI should be 100 (no losses at all).
    rsi_values = rsi_values.where(avg_loss != 0, 100.0)

    return rsi_values


def macd(
    series: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> pd.DataFrame:
    """
    Moving Average Convergence Divergence.

    Returns a DataFrame with columns: macd, signal, histogram.
    - macd crossing above signal = bullish momentum
    - macd crossing below signal = bearish momentum
    """
    fast_ema = ema(series, fast_period)
    slow_ema = ema(series, slow_period)

    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line

    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": histogram,
        }
    )


def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """
    Bollinger Bands.

    Returns a DataFrame with columns: middle, upper, lower.
    - Price near upper band = relatively expensive / possible reversal down
    - Price near lower band = relatively cheap / possible reversal up
    """
    middle = sma(series, period)
    std = series.rolling(window=period).std()

    upper = middle + (num_std * std)
    lower = middle - (num_std * std)

    return pd.DataFrame({"middle": middle, "upper": upper, "lower": lower})


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """
    Average True Range -- a volatility measure, useful later for
    setting stop-loss / take-profit distances relative to current
    market conditions instead of fixed pip values.
    """
    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convenience function: given a DataFrame with columns
    ['open', 'high', 'low', 'close'], returns a new DataFrame with all
    indicators appended as extra columns.
    """
    out = df.copy()

    out["ema_9"] = ema(out["close"], 9)
    out["ema_21"] = ema(out["close"], 21)
    out["ema_50"] = ema(out["close"], 50)
    out["ema_14"] = ema(out["close"], 14)

    out["rsi_14"] = rsi(out["close"], 14)

    macd_df = macd(out["close"])
    out["macd"] = macd_df["macd"]
    out["macd_signal"] = macd_df["signal"]
    out["macd_hist"] = macd_df["histogram"]

    bb_df = bollinger_bands(out["close"])
    out["bb_middle"] = bb_df["middle"]
    out["bb_upper"] = bb_df["upper"]
    out["bb_lower"] = bb_df["lower"]

    out["atr_14"] = atr(out["high"], out["low"], out["close"], 14)

    return out