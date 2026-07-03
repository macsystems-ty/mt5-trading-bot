"""
candle_builder.py

Aggregates raw tick data into OHLC candles for multiple timeframes
simultaneously (1min, 5min, 15min), from a single stream of ticks.

This is meant to be imported and used by the live streaming script —
each tick gets fed into `CandleAggregator.add_tick(...)`, and whenever
a candle for a given timeframe closes, the provided callback fires
with the completed candle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional


# Timeframes we support, expressed in seconds.
TIMEFRAMES_SECONDS: Dict[str, int] = {
    "1min": 60,
    "5min": 5 * 60,
    "15min": 15 * 60,
}


@dataclass
class Candle:
    timeframe: str
    open_time: datetime  # start of the candle's time bucket (UTC)
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.tick_count += 1

    def as_dict(self) -> dict:
        return {
            "timeframe": self.timeframe,
            "open_time": self.open_time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "tick_count": self.tick_count,
        }


@dataclass
class CandleAggregator:
    """
    Builds candles for every timeframe in TIMEFRAMES_SECONDS at once.

    on_candle_close: callback invoked as on_candle_close(candle) whenever
    a candle for any timeframe finishes and a new one begins.
    """

    on_candle_close: Optional[Callable[[Candle], None]] = None

    _current_candles: Dict[str, Candle] = field(default_factory=dict)

    # Keeps a rolling history per timeframe, capped at `history_limit`,
    # so the strategy/indicator layer always has recent candles to work with.
    history_limit: int = 500
    _history: Dict[str, List[Candle]] = field(
        default_factory=lambda: {tf: [] for tf in TIMEFRAMES_SECONDS}
    )

    def _bucket_start(self, epoch: float, timeframe_seconds: int) -> datetime:
        """Round an epoch timestamp down to the start of its candle bucket."""
        bucket_epoch = int(epoch // timeframe_seconds) * timeframe_seconds
        return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)

    def add_tick(self, epoch: float, price: float) -> None:
        """Feed a single tick (epoch seconds + price) into all timeframes."""
        for timeframe, seconds in TIMEFRAMES_SECONDS.items():
            bucket_start = self._bucket_start(epoch, seconds)
            current = self._current_candles.get(timeframe)

            if current is None:
                # First candle ever for this timeframe.
                self._current_candles[timeframe] = Candle(
                    timeframe=timeframe,
                    open_time=bucket_start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    tick_count=1,
                )
                continue

            if bucket_start == current.open_time:
                # Same candle — just update it.
                current.update(price)
            else:
                # New bucket has started -> the previous candle is closed.
                self._close_candle(timeframe, current)

                # Start a fresh candle for the new bucket.
                self._current_candles[timeframe] = Candle(
                    timeframe=timeframe,
                    open_time=bucket_start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    tick_count=1,
                )

    def _close_candle(self, timeframe: str, candle: Candle) -> None:
        history = self._history[timeframe]
        history.append(candle)
        if len(history) > self.history_limit:
            history.pop(0)

        if self.on_candle_close:
            self.on_candle_close(candle)

    def get_history(self, timeframe: str) -> List[Candle]:
        """Return the closed-candle history for a timeframe (oldest -> newest)."""
        return list(self._history[timeframe])

    def get_current(self, timeframe: str) -> Optional[Candle]:
        """Return the in-progress (still forming) candle for a timeframe."""
        return self._current_candles.get(timeframe)
