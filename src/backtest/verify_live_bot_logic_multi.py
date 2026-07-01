"""
verify_live_bot_logic_multi.py

Same proven replay logic as verify_live_bot_logic.py, but
parameterized to test ANY of our market-specific live bot copies
(in live_bots/<SYMBOL>/) against their corresponding historical data
(in markets/<SYMBOL>/data/).

Run with:
    python verify_live_bot_logic_multi.py R_100
    python verify_live_bot_logic_multi.py 1HZ75V
    python verify_live_bot_logic_multi.py 1HZ90V
    python verify_live_bot_logic_multi.py 1HZ100V
"""

import asyncio
import importlib
import os
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MARKET_SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "R_100"

LIVE_BOT_DIR = os.path.join(REPO_ROOT, "live_bots", MARKET_SYMBOL)
HISTORICAL_DATA_DIR = os.path.join(REPO_ROOT, "markets", MARKET_SYMBOL, "data")

if not os.path.isdir(LIVE_BOT_DIR):
    print(f"ERROR: {LIVE_BOT_DIR} not found. Check the market symbol and folder structure.")
    sys.exit(1)

sys.path.insert(0, LIVE_BOT_DIR)

if "live_mt5_trading_bot" in sys.modules:
    del sys.modules["live_mt5_trading_bot"]
bot = importlib.import_module("live_mt5_trading_bot")


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
        self._state_ref = state_ref

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
    path_5min = os.path.join(HISTORICAL_DATA_DIR, f"{MARKET_SYMBOL}_5min.csv")
    if not os.path.exists(path_5min):
        print(f"ERROR: {path_5min} not found. Run fetch_historical.py for this market first.")
        sys.exit(1)

    candles_5min = pd.read_csv(path_5min, index_col="open_time", parse_dates=True).sort_index()

    candles_1h = (
        candles_5min.resample("1h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )

    print(f"Testing live bot logic for MARKET: {MARKET_SYMBOL}")
    print(f"  Live bot path: {LIVE_BOT_DIR}")
    print(f"  Data path: {path_5min}")
    print(f"Replaying {len(candles_5min):,} 5min candles through the LIVE bot's actual functions ...")

    trades = asyncio.run(replay(candles_5min, candles_1h))

    decided = [t for t in trades if t.pct_change() is not None]
    total_return = sum(t.pct_change() for t in decided)
    wins = sum(1 for t in decided if t.pct_change() > 0)
    win_rate = (wins / len(decided) * 100) if decided else 0

    print(f"\nLive bot logic replay results for {MARKET_SYMBOL}:")
    print(f"  Trades: {len(decided):,}")
    print(f"  Win rate: {win_rate:.1f}%")
    print(f"  Total % return (no spread/commission modeled here): {total_return:+.3f}%")

    print(
        "\nCompare this trade COUNT and general shape against this market's trusted\n"
        "backtester result (from backtest_strategy.py / pre_live_validation_report.py).\n"
        "A wildly different trade count or win rate would signal a real bug in this\n"
        "market's live bot copy that needs fixing before trusting it live."
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
