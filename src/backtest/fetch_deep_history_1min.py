"""
fetch_deep_history_1min.py

Dedicated fetcher for 1min candles, targeting roughly 6 MONTHS of
history (not a full year, since 1min data is ~5x denser than 5min and
a full year would be a very large fetch). Pagination will STOP ON ITS
OWN once Deriv has no more history to give (the real ceiling for 1min
granularity may differ from the ~1 year we found for 5min candles).

Run with:
    python src/backtest/fetch_deep_history_1min.py
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
SYMBOL = os.getenv("DERIV_SYMBOL", "1HZ25V")

DERIV_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"

GRANULARITY = 60

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

MAX_PAGES = 280


def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {message}")


async def fetch_candles(symbol: str, granularity: int, count: int, end_epoch: int) -> list:
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


def load_existing() -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"{SYMBOL}_1min.csv")
    if not os.path.exists(path):
        return pd.DataFrame(columns=["open", "high", "low", "close"]).rename_axis("open_time")
    return pd.read_csv(path, index_col="open_time", parse_dates=True)


async def main_async() -> None:
    if not APP_ID:
        raise RuntimeError("DERIV_APP_ID not found. Make sure it's set in your .env file.")

    os.makedirs(DATA_DIR, exist_ok=True)

    existing = load_existing()

    if existing.empty:
        log("No existing 1min data found -- run fetch_history.py first to get an initial batch.")
        return

    oldest_time = existing.index.min()
    log(f"Current 1min range starts at {oldest_time}. Paginating backward (up to {MAX_PAGES} pages)...")
    log("Target: ~262,800 candles for 6 months of 1min history (script will stop earlier if Deriv's real history limit is reached first).\n")

    all_new_frames = []
    end_epoch = int(oldest_time.timestamp()) - 1
    pages_fetched = 0

    for page in range(MAX_PAGES):
        try:
            candles = await fetch_candles(SYMBOL, GRANULARITY, 1000, end_epoch)
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
        all_new_frames.append(df_page)
        pages_fetched += 1

        new_oldest = df_page.index.min()

        if (page + 1) % 10 == 0 or page == 0:
            total_so_far = len(existing) + sum(len(f) for f in all_new_frames)
            log(
                f"Page {page + 1}/{MAX_PAGES}: now have ~{total_so_far:,} total candles "
                f"(oldest: {new_oldest})"
            )

        if new_oldest >= oldest_time:
            log(f"Pagination stalled (no further progress) at page {page + 1} -- stopping.")
            break

        end_epoch = int(new_oldest.timestamp()) - 1
        oldest_time = new_oldest

        await asyncio.sleep(1)

    if not all_new_frames:
        log("No additional history retrieved this run.")
        return

    combined = pd.concat([existing] + all_new_frames)
    combined = combined[~combined.index.duplicated(keep="first")]
    combined = combined.sort_index()

    output_path = os.path.join(DATA_DIR, f"{SYMBOL}_1min.csv")
    combined.to_csv(output_path)

    log(
        f"\nFINAL RESULT: saved {len(combined):,} total 1min candles -> {output_path}\n"
        f"Range: {combined.index[0]} to {combined.index[-1]}\n"
        f"Fetched {pages_fetched} pages this run."
    )

    if len(combined) < 262800:
        log(
            f"\nNote: this is short of a full 6 months of 1min candles "
            f"(~262,800 expected, {len(combined):,} obtained). This likely "
            f"means Deriv's available history for 1min granularity on this "
            f"symbol is shorter than 6 months, OR pagination stopped early "
            f"for another reason -- see the log above for why it stopped."
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
