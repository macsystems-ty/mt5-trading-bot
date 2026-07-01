"""
list_gold_bitcoin_symbols.py

Lists all available trading symbols on your connected MT5 account,
filtered for anything related to GOLD or BITCOIN.

Run with:
    python src/live/list_gold_bitcoin_symbols.py
"""

import asyncio
import os

from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

load_dotenv()

METAAPI_TOKEN = os.getenv("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")


async def main() -> None:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        print("ERROR: METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set in your .env file.")
        return

    api = MetaApi(token=METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    if account.state != "DEPLOYED":
        await account.deploy()

    await account.wait_connected()

    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    print("Fetching full symbol list ...")
    symbols = await connection.get_symbols()
    print(f"Total symbols available: {len(symbols)}\n")

    gold_keywords = ["gold", "xau"]
    bitcoin_keywords = ["bitcoin", "btc", "crypto"]

    gold_matches = [s for s in symbols if any(k in s.lower() for k in gold_keywords)]
    bitcoin_matches = [s for s in symbols if any(k in s.lower() for k in bitcoin_keywords)]

    print(f"GOLD-related symbols ({gold_keywords}):")
    if gold_matches:
        for s in gold_matches:
            print(f"  {s}")
    else:
        print("  No matches found.")

    print(f"\nBITCOIN/CRYPTO-related symbols ({bitcoin_keywords}):")
    if bitcoin_matches:
        for s in bitcoin_matches:
            print(f"  {s}")
    else:
        print("  No matches found.")

    if not gold_matches and not bitcoin_matches:
        print("\nNo matches at all -- printing first 80 symbols so we can spot the naming pattern:")
        for s in symbols[:80]:
            print(f"  {s}")


if __name__ == "__main__":
    asyncio.run(main())
