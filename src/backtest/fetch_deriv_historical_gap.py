"""
fetch_deriv_historical_gap.py

Fetches 5-minute candle data directly from Deriv's PUBLIC websocket
API (no authentication required) for the symbol "1HZ25V"
(Volatility 25 (1s) Index), covering the gap between our existing
historical CSV's end date and now.

Uses the documented `ticks_history` request with
style="candles", granularity=300 (5 minutes in seconds).

Saves output in the EXACT same CSV format as our existing historical
files (open_time,open,high,low,close), so it can be directly
appended to src/backtest/data/1HZ25V_5min.csv to backfill the gap.

NOTE: this requires network access and the `websockets` package
(pip install websockets --break-system-packages). It was written
and reasoned through carefully, but could not be tested end-to-end
in the development sandbox (no network access there) -- please run
it and report back any errors so we can fix them together.

Run with:
    python src/backtest/fetch_deriv_historical_gap.py
"""

import asyncio
import csv
import json
import sys
from datetime import datetime, timezone

import websockets

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL = "1HZ25V"
GRANULARITY_SECONDS = 300  # 5 minutes
MAX_CANDLES_PER_REQUEST = 5000  # Deriv's documented max per request

# EDIT THESE: the gap to backfill. Use UTC epoch seconds.
START_TIME_STR = "2026-06-21 22:25:00"  # matches the end of our existing historical CSV
END_TIME_STR = "latest"  # "latest" = up to now

OUTPUT_CSV_PATH = "deriv_gap_fill_5min.csv"


def to_epoch(time_str: str) -> int:
    dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


async def fetch_candles(start_epoch: int, end_value) -> list:
    all_candles = []
    current_start = start_epoch

    async with websockets.connect(DERIV_WS_URL) as ws:
        while True:
            request = {
                "ticks_history": SYMBOL,
                "start": current_start,
                "end": end_value,
                "style": "candles",
                "granularity": GRANULARITY_SECONDS,
                "count": MAX_CANDLES_PER_REQUEST,
                "adjust_start_time": 1,
            }
            await ws.send(json.dumps(request))
            response_raw = await ws.recv()
            response = json.loads(response_raw)

            if "error" in response:
                print(f"ERROR from Deriv API: {response['error']}")
                break

            candles = response.get("candles", [])
            if not candles:
                print("No more candles returned -- stopping.")
                break

            all_candles.extend(candles)
            print(f"  Fetched {len(candles)} candles "
                  f"(total so far: {len(all_candles)}), "
                  f"last epoch: {candles[-1]['epoch']}")

            if len(candles) < MAX_CANDLES_PER_REQUEST:
                break

            current_start = candles[-1]["epoch"] + GRANULARITY_SECONDS

            if end_value != "latest" and current_start >= end_value:
                break

    return all_candles


def save_to_csv(candles: list, path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["open_time", "open", "high", "low", "close"])
        for c in candles:
            dt = datetime.fromtimestamp(c["epoch"], tz=timezone.utc)
            writer.writerow([dt.isoformat(), c["open"], c["high"], c["low"], c["close"]])
    print(f"\nSaved {len(candles)} candles to {path}")


async def main() -> None:
    start_epoch = to_epoch(START_TIME_STR)
    end_value = "latest" if END_TIME_STR == "latest" else to_epoch(END_TIME_STR)

    print(f"Fetching {SYMBOL} 5min candles from {START_TIME_STR} to {END_TIME_STR} ...")
    candles = await fetch_candles(start_epoch, end_value)

    if not candles:
        print("No candles fetched. Check the symbol name and time range.")
        sys.exit(1)

    save_to_csv(candles, OUTPUT_CSV_PATH)
    print(
        f"\nNext step: review {OUTPUT_CSV_PATH}, then append its rows (after the\n"
        f"header) to the end of src/backtest/data/1HZ25V_5min.csv to backfill\n"
        f"the gap, making sure there's no overlap/duplicate timestamps at the\n"
        f"boundary."
    )


if __name__ == "__main__":
    asyncio.run(main())
