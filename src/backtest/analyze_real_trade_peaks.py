"""
analyze_real_trade_peaks.py

For each REAL closed trade in the last few days (pulled from the
broker), replays that trade's actual price path using the historical
5-minute candle data, to find the REAL peak profit reached (in
dollars) before the trade was finally closed.

Requires the relevant market's CSV in markets/<SYMBOL>/data/ to be
reasonably up to date (covering the trade dates being checked).

Run with:
    python analyze_real_trade_peaks.py
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

load_dotenv()

METAAPI_TOKEN = os.getenv("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")

DAYS_TO_LOOK_BACK = 4

SYMBOL_TO_FOLDER = {
    "Volatility 25 (1s) Index": "1HZ25V",
    "Volatility 75 (1s) Index": "1HZ75V",
    "Volatility 90 (1s) Index": "1HZ90V",
    "Volatility 100 (1s) Index": "1HZ100V",
    "Volatility 100 Index": "R_100",
}

MARKETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "markets")


def load_candles_for_symbol(folder_name):
    csv_path = os.path.join(MARKETS_DIR, folder_name, "data", f"{folder_name}_5min.csv")
    if not os.path.exists(csv_path):
        return None
    df = pd.read_csv(csv_path, parse_dates=["open_time"])
    df = df.set_index("open_time").sort_index()
    return df


def find_peak_profit(candles, direction, entry_price, entry_time, exit_time, volume):
    window = candles[(candles.index >= entry_time) & (candles.index <= exit_time)]
    if window.empty:
        return None, None

    if direction == "BUY":
        peak_price = window["high"].max()
        peak_pct = (peak_price - entry_price) / entry_price * 100
    else:
        peak_price = window["low"].min()
        peak_pct = (entry_price - peak_price) / entry_price * 100

    peak_dollar = volume * abs(peak_price - entry_price)
    return peak_pct, peak_dollar


async def main() -> None:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        print("ERROR: METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set in your .env file.")
        return

    api = MetaApi(token=METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(account_id=METAAPI_ACCOUNT_ID)
    connection = account.get_streaming_connection()

    print("Connecting and synchronizing (this can take a moment) ...")
    await connection.connect()
    await connection.wait_synchronized()

    history_storage = connection.history_storage
    deals = history_storage.deals

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=DAYS_TO_LOOK_BACK)

    opens_by_position = {}
    closes_by_position = {}
    for d in deals:
        pos_id = d.get("positionId")
        entry_type = d.get("entryType")
        if entry_type == "DEAL_ENTRY_IN":
            opens_by_position[pos_id] = d
        elif entry_type == "DEAL_ENTRY_OUT":
            closes_by_position[pos_id] = d

    print(f"\nDIAGNOSTIC: total deals in history storage: {len(deals)}")
    print(f"DIAGNOSTIC: opens found: {len(opens_by_position)}, closes found: {len(closes_by_position)}")

    closes_in_window = 0
    for pos_id, close_deal in closes_by_position.items():
        close_time = close_deal.get("time")
        if isinstance(close_time, str):
            close_time = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        if close_time >= start_time:
            closes_in_window += 1
    print(f"DIAGNOSTIC: closes within the last {DAYS_TO_LOOK_BACK} days: {closes_in_window}")
    print(f"DIAGNOSTIC: MARKETS_DIR resolves to: {os.path.abspath(MARKETS_DIR)}")
    print(f"DIAGNOSTIC: MARKETS_DIR exists: {os.path.isdir(MARKETS_DIR)}\n")

    print(f"{'=' * 110}")
    print(f"REAL TRADES: peak profit reached vs. final closed P&L (last {DAYS_TO_LOOK_BACK} days)")
    print(f"{'=' * 110}")
    print(
        f"{'Symbol':>26} | {'Dir':>4} | {'Open Time':>20} | {'Peak Profit':>12} | "
        f"{'Final P&L':>10} | {'Gave Back':>10}"
    )
    print("-" * 110)

    candles_cache = {}
    rows_printed = 0

    for pos_id, close_deal in sorted(closes_by_position.items(), key=lambda kv: kv[1].get("time", "")):
        open_deal = opens_by_position.get(pos_id)
        if open_deal is None:
            continue

        close_time = close_deal.get("time")
        if isinstance(close_time, str):
            close_time = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        if close_time < start_time:
            continue

        symbol = close_deal.get("symbol", "")
        folder_name = SYMBOL_TO_FOLDER.get(symbol)
        if folder_name is None:
            continue

        if folder_name not in candles_cache:
            candles_cache[folder_name] = load_candles_for_symbol(folder_name)
        candles = candles_cache[folder_name]
        if candles is None:
            continue

        direction = "BUY" if open_deal.get("type") == "DEAL_TYPE_BUY" else "SELL"
        entry_price = open_deal.get("price")
        volume = open_deal.get("volume", 0)
        open_time = open_deal.get("time")
        if isinstance(open_time, str):
            open_time = datetime.fromisoformat(open_time.replace("Z", "+00:00"))

        final_pnl = (
            float(close_deal.get("profit", 0))
            + float(close_deal.get("commission", 0))
            + float(close_deal.get("swap", 0))
        )

        peak_pct, peak_dollar = find_peak_profit(candles, direction, entry_price, open_time, close_time, volume)
        if peak_pct is None:
            continue

        gave_back = peak_dollar - final_pnl if peak_dollar is not None else None

        print(
            f"{symbol:>26} | {direction:>4} | {str(open_time):>20} | "
            f"${peak_dollar:>+10.2f} | ${final_pnl:>+8.2f} | ${gave_back:>+8.2f}"
        )
        rows_printed += 1

    if rows_printed == 0:
        print(
            "No rows printed -- this usually means the local historical CSVs in "
            "markets/<SYMBOL>/data/ don't cover the trade dates being checked, or "
            "the symbol name mapping (SYMBOL_TO_FOLDER) needs updating."
        )

    print(
        "\nNOTE: 'Peak Profit' is computed from the HISTORICAL CANDLE DATA's high/low "
        "during the trade's lifetime. 'Gave Back' = Peak Profit - Final P&L -- a LARGE "
        "positive number means the trade reached a much bigger profit before the "
        "trailing stop caught it on the way back down."
    )

    await connection.close()


if __name__ == "__main__":
    asyncio.run(main())