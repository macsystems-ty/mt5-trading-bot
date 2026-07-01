"""
verify_live_bot_logic.py

Imports the ACTUAL functions from live_mt5_trading_bot.py (not a
reimplementation) and replays them against historical 5min data using
a MOCK connection object that records what orders WOULD have been
placed, without placing any real ones. Compares the resulting trades
against what our trusted backtester found, to prove the live bot's
port of the strategy logic is correct.

Run with:
    python src/backtest/verify_live_bot_logic.py
"""

import asyncio
import os
import sys

import pandas as pd

LIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "live")
sys.path.insert(0, LIVE_PATH)

import live_mt5_trading_bot as bot  # noqa: E402

HISTORICAL_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOL_PREFIX = "1HZ25V"


class RecordedTrade:
    def __init__(self, direction, entry_time, entry_price, exit_time=None, exit_price=None):
        self.direction = direction
        self.entry_time = entry_time
        self.entry_price = entry_price
        self.exit_time = exit_time
        self.exit_price = exit_price

    def pct_change(self):
        if self.exit_price is None:
            return None
        if self.direction == "BUY":
            return (self.exit_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - self.exit_price) / self.entry_price * 100


class MockConnection:
    def __init__(self, state_ref):
        self.recorded_trades = []
        self._next_position_id = 1
        self._open_trade_record = None
        self._current_time = None
        self._current_price = None
        self._state_ref = state_ref  # reference to the live BotState, to read the real exit level

    async def create_market_buy_order(self, symbol, volume, options=None):
        position_id = str(self._next_position_id)
        self._next_position_id += 1
        self._open_trade_record = RecordedTrade(
            direction="BUY", entry_time=self._current_time, entry_price=self._current_price
        )
        return {"positionId": position_id}

    async def create_market_sell_order(self, symbol, volume, options=None):
        position_id = str(self._next_position_id)
        self._next_position_id += 1
        self._open_trade_record = RecordedTrade(
            direction="SELL", entry_time=self._current_time, entry_price=self._current_price
        )
        return {"positionId": position_id}

    async def close_position(self, position_id):
        if self._open_trade_record is not None:
            self._open_trade_record.exit_time = self._current_time
            # Use the REAL trailing_stop level the bot's logic decided to
            # exit at (still present on state.open_position at this point,
            # since close_trade clears it only AFTER calling this), not
            # the candle's close -- a candle can close well away from the
            # stop level that actually triggered the exit mid-candle, and
            # using the close price there was silently inflating win rate.
            open_position = self._state_ref.open_position
            if open_position is not None:
                self._open_trade_record.exit_price = open_position.trailing_stop
            else:
                self._open_trade_record.exit_price = self._current_price
            self.recorded_trades.append(self._open_trade_record)
            self._open_trade_record = None
        return {"positionId": position_id}


async def replay(candles_5min: pd.DataFrame, candles_1h: pd.DataFrame) -> list:
    state = bot.BotState()
    connection = MockConnection(state)

    all_candles = []
    for open_time, row in candles_5min.iterrows():
        all_candles.append(
            bot.Candle(
                timeframe="5min", open_time=open_time.to_pydatetime(),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), tick_count=0,
            )
        )

    all_1h_candles = []
    for open_time, row in candles_1h.iterrows():
        all_1h_candles.append(
            bot.Candle(
                timeframe="1h", open_time=open_time.to_pydatetime(),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), tick_count=0,
            )
        )

    state.current_balance = 1000.0

    warmup_5min = 100
    warmup_1h = 51

    for c in all_candles[:warmup_5min]:
        state.candles_5min.append(c)
        bot.update_swing_levels(state, quiet=True)
    state.candles_1h = all_1h_candles[:warmup_1h]

    h1_index = warmup_1h

    for i in range(warmup_5min, len(all_candles)):
        candle = all_candles[i]
        state.candles_5min.append(candle)
        state.candles_5min = state.candles_5min[-2000:]

        connection._current_time = candle.open_time
        connection._current_price = candle.close

        if h1_index < len(all_1h_candles) and candle.open_time >= all_1h_candles[h1_index].open_time:
            state.candles_1h.append(all_1h_candles[h1_index])
            state.candles_1h = state.candles_1h[-500:]
            h1_index += 1

        bot.update_swing_levels(state, quiet=True)

        if state.open_position is not None:
            await bot.manage_open_position(connection, state)
        else:
            await bot.check_for_entry(connection, state)

    return connection.recorded_trades


def main() -> None:
    path_5min = os.path.join(HISTORICAL_DATA_DIR, f"{SYMBOL_PREFIX}_5min.csv")
    candles_5min = pd.read_csv(path_5min, index_col="open_time", parse_dates=True).sort_index()

    candles_1h = (
        candles_5min.resample("1h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )

    print(f"Replaying {len(candles_5min)} 5min candles through the LIVE bot's actual functions ...")
    trades = asyncio.run(replay(candles_5min, candles_1h))

    decided = [t for t in trades if t.pct_change() is not None]
    total_return = sum(t.pct_change() for t in decided)
    wins = sum(1 for t in decided if t.pct_change() > 0)
    win_rate = (wins / len(decided) * 100) if decided else 0

    print("\nLive bot logic replay results:")
    print(f"  Trades: {len(decided)}")
    print(f"  Win rate: {win_rate:.1f}%")
    print(f"  Total % return (no spread/commission modeled here): {total_return:+.3f}%")

    print(
        "\nCompare this trade COUNT and general shape against our trusted backtester's "
        "result on the same data (10-pattern + 3-stage exit strategy: 5,838 trades, "
        "43.7% win rate, +145.252% full-history return). Exact numbers won't "
        "match perfectly (different warmup, no spread modeled here, slightly different "
        "candle indexing) -- but a wildly different trade count or win rate would signal "
        "a real bug in the live bot's logic that needs fixing before trusting it live."
    )

    if decided:
        print("\nFirst 10 trades:")
        for t in decided[:10]:
            print(
                f"  {t.entry_time} {t.direction} @ {t.entry_price:.2f} -> "
                f"{t.exit_time} @ {t.exit_price:.2f} ({t.pct_change():+.3f}%)"
            )


if __name__ == "__main__":
    main()