"""
fetch_historical.py

Fetches 5-minute candle data for THIS market's symbol using
BACKWARD PAGINATION -- repeatedly requesting "the N candles ending
right before what we already have" -- since a single start/end
range request hits Deriv's per-request limit quickly (caps out
around ~18 days of 5min candles per request, regardless of symbol).
This is the same proven strategy that successfully obtained a full
year of history for our original 1HZ25V dataset.

Requires DERIV_APP_ID in your .env file.

Run with:
    python fetch_historical.py
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import pandas as pd
import websockets
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.getenv("DERIV_APP_ID")

# EDIT THIS for each market folder.
SYMBOL = "R_100"

DERIV_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
GRANULARITY = 300  # 5 minutes

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUTPUT_PATH = os.path.join(DATA_DIR, f"{SYMBOL}_5min.csv")

CANDLES_PER_PAGE = 5000
MAX_PAGES = 100


def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {message}")


async def fetch_candles_page(symbol: str, granularity: int, count: int, end_epoch) -> list:
    headers = {"Deriv-App-ID": APP_ID} if APP_ID else {}

    async with websockets.connect(DERIV_WS_URL, additional_headers=headers) as ws:
        request = {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": count,
            "end": str(end_epoch),
        }
        await ws.send(json.dumps(request))
        response = json.loads(await ws.recv())

        if "error" in response or "errors" in response:
            raise RuntimeError(f"API error fetching history: {response}")

        candles = response.get("candles")
        if candles is None:
            raise RuntimeError(f"Unexpected response shape: {response}")

        return candles


def candles_to_dataframe(candles: list) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["open_time"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    df = df[["open_time", "open", "high", "low", "close"]]
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    return df.set_index("open_time")


async def main_async() -> None:
    if not APP_ID:
        raise RuntimeError("DERIV_APP_ID not found. Make sure it's set in your .env file.")

    os.makedirs(DATA_DIR, exist_ok=True)

    log(f"Fetching {SYMBOL} 5min candles via backward pagination ...")

    all_frames = []
    end_epoch = "latest"
    pages_fetched = 0
    oldest_seen = None

    for page in range(MAX_PAGES):
        try:
            candles = await fetch_candles_page(SYMBOL, GRANULARITY, CANDLES_PER_PAGE, end_epoch)
        except RuntimeError as exc:
            log(f"Stopped paginating at page {page + 1}: {exc}")
            break

        if not candles:
            log(
                f"No more candles returned at page {page + 1} -- this is the REAL "
                f"history limit for this symbol/timeframe on this API."
            )
            break

        df_page = candles_to_dataframe(candles)
        all_frames.append(df_page)
        pages_fetched += 1

        new_oldest = df_page.index.min()

        log(
            f"Page {page + 1}/{MAX_PAGES}: fetched {len(df_page)} candles "
            f"(oldest so far: {new_oldest})"
        )

        if oldest_seen is not None and new_oldest >= oldest_seen:
            log(f"Pagination stalled (no further progress) at page {page + 1} -- stopping.")
            break

        oldest_seen = new_oldest
        end_epoch = int(new_oldest.timestamp()) - 1

        await asyncio.sleep(1)

    if not all_frames:
        log("No history retrieved.")
        return

    combined = pd.concat(all_frames)
    combined = combined[~combined.index.duplicated(keep="first")]
    combined = combined.sort_index()

    combined.to_csv(OUTPUT_PATH)

    log(
        f"\nFINAL RESULT: saved {len(combined):,} total candles -> {OUTPUT_PATH}\n"
        f"Range: {combined.index[0]} to {combined.index[-1]}\n"
        f"Fetched {pages_fetched} page(s) this run."
    )


def main() -> None:
    try:
        asyncio.run(main_async())
    except RuntimeError as exc:
        log(f"Fatal error: {exc}")
    except Exception as exc:  # noqa: BLE001
        log(f"Unexpected error: {exc!r}")


if __name__ == "__main__":
    main()
