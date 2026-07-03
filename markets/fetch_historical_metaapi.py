"""
fetch_historical_metaapi.py

Fetches historical 5-minute candle data for a REAL MT5-traded symbol
(XAUUSD, BTCUSD, etc.) directly from MetaApi's historical market
data REST endpoint -- the correct data source for real broker-traded
symbols, as opposed to Deriv's separate synthetic-index API used for
the Volatility indices.

IMPORTANT: written carefully based on MetaApi's documented REST API,
but could NOT be tested end-to-end in the development sandbox (no
network access there). Please run it and report back any errors.

Run with:
    python fetch_historical_metaapi.py XAUUSD
    python fetch_historical_metaapi.py BTCUSD
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

METAAPI_TOKEN = os.getenv("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
TIMEFRAME = "5m"
DAYS_OF_HISTORY = 365
CANDLES_PER_PAGE = 1000

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), SYMBOL, "data")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, f"{SYMBOL}_5min.csv")

# Hosted on a DIFFERENT hostname than other MetaApi endpoints, and is
# region-specific ("new-york" per MetaApi's documented example). If
# your account's region differs, this may need adjusting.
HISTORICAL_DATA_BASE_URL = "https://mt-market-data-client-api-v1.new-york.agiliumtrade.ai"


async def fetch_candles_page(session, account_id, symbol, timeframe, end_time, token):
    url = (
        f"{HISTORICAL_DATA_BASE_URL}/users/current/accounts/{account_id}"
        f"/historical-market-data/symbols/{symbol}/timeframes/{timeframe}/candles"
    )
    params = {
        "startTime": end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "limit": CANDLES_PER_PAGE,
    }
    headers = {"auth-token": token, "Accept": "application/json"}

    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=240)) as response:
        if response.status != 200:
            body = await response.text()
            raise RuntimeError(f"API error (status {response.status}): {body}")
        return await response.json()


async def main_async() -> None:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        raise RuntimeError("METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set in your .env file.")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"Fetching {SYMBOL} {TIMEFRAME} candles for the last {DAYS_OF_HISTORY} days via MetaApi ...")

    all_candles = []
    end_time = datetime.now(timezone.utc)
    earliest_wanted = end_time - timedelta(days=DAYS_OF_HISTORY)

    async with aiohttp.ClientSession() as session:
        page = 0
        while end_time > earliest_wanted:
            page += 1
            try:
                candles = await fetch_candles_page(
                    session, METAAPI_ACCOUNT_ID, SYMBOL, TIMEFRAME, end_time, METAAPI_TOKEN
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  Page {page}: ERROR ({exc!r}) -- stopping pagination.")
                break

            if not candles:
                print(f"  Page {page}: no more candles returned -- this is the real history limit.")
                break

            all_candles.extend(candles)
            oldest_in_page = min(c["time"] for c in candles)
            oldest_dt = datetime.fromisoformat(oldest_in_page.replace("Z", "+00:00"))
            print(f"  Page {page}: fetched {len(candles)} candles (oldest so far: {oldest_dt})")

            if len(candles) < CANDLES_PER_PAGE:
                break

            end_time = oldest_dt - timedelta(seconds=1)
            await asyncio.sleep(1)

    if not all_candles:
        print("No candles fetched.")
        return

    df = pd.DataFrame(all_candles)
    df["open_time"] = pd.to_datetime(df["time"])
    df = df[["open_time", "open", "high", "low", "close"]]
    df = df.drop_duplicates(subset="open_time").sort_values("open_time")
    df.to_csv(OUTPUT_PATH, index=False)

    print(
        f"\nSaved {len(df):,} candles -> {OUTPUT_PATH}\n"
        f"Range: {df['open_time'].iloc[0]} to {df['open_time'].iloc[-1]}"
    )


def main() -> None:
    try:
        asyncio.run(main_async())
    except Exception as exc:  # noqa: BLE001
        print(f"Fatal error: {exc!r}")


if __name__ == "__main__":
    main()
