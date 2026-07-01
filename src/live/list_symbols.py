"""
list_symbols.py

Lists all available trading symbols on your connected MT5 account,
and specifically filters for anything containing "Volatility" or "VIX"
or "25" -- to find the correct symbol name/format for Volatility 25
(1s) on MT5 (which may differ from the "1HZ25V" code used by Deriv's
Options API).

Run with:
    python src/live/list_symbols.py
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

    keywords = ["volatility", "vix", "v25", "v75", "v100", "1hz"]
    matches = [
        s for s in symbols
        if any(keyword in s.lower() for keyword in keywords)
    ]

    print(f"Symbols matching our keywords ({keywords}):")
    if matches:
        for s in matches:
            print(f"  {s}")
    else:
        print("  No matches found with these keywords.")
        print("\nPrinting first 50 symbols overall so we can spot the naming pattern:")
        for s in symbols[:50]:
            print(f"  {s}")


if __name__ == "__main__":
    asyncio.run(main())
