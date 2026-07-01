"""
test_metaapi_connection.py

First connectivity test for the MT5 trading bot. Confirms we can:
  1. Authenticate with MetaApi using your account token
  2. Connect to your actual Deriv MT5 account (via MetaApi's cloud)
  3. Retrieve account info (balance, equity) and open positions

This does NOT place any trades -- read-only checks only, to confirm
the connection works before we build anything that touches real
orders.

Run with:
    python src/live/test_metaapi_connection.py
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

    print("Connecting to MetaApi ...")
    api = MetaApi(token=METAAPI_TOKEN)

    print(f"Fetching account {METAAPI_ACCOUNT_ID} ...")
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    print(f"Account state: {account.state}")
    print(f"Account connection status: {account.connection_status}")

    if account.state != "DEPLOYED":
        print("Account is not deployed yet -- deploying now ...")
        await account.deploy()

    print("Waiting for account connection to broker ...")
    await account.wait_connected()

    print("Connected. Establishing RPC connection ...")
    connection = account.get_rpc_connection()
    await connection.connect()

    print("Waiting for terminal state synchronization (this can take a moment) ...")
    await connection.wait_synchronized()

    print("\n--- ACCOUNT INFORMATION ---")
    account_info = await connection.get_account_information()
    print(f"Balance: {account_info.get('balance')} {account_info.get('currency')}")
    print(f"Equity: {account_info.get('equity')}")
    print(f"Leverage: {account_info.get('leverage')}")
    print(f"Server: {account_info.get('server')}")
    print(f"Login: {account_info.get('login')}")

    print("\n--- OPEN POSITIONS ---")
    positions = await connection.get_positions()
    if not positions:
        print("No open positions.")
    else:
        for pos in positions:
            print(
                f"  {pos.get('symbol')} | {pos.get('type')} | "
                f"volume={pos.get('volume')} | profit={pos.get('profit')}"
            )

    print("\n✅ Connection test complete -- everything above worked correctly.")


if __name__ == "__main__":
    asyncio.run(main())
