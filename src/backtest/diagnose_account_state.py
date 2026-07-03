"""
diagnose_account_state.py

Queries your MT5 account's ACTUAL current state directly from
MetaApi's account management API (not the streaming connection our
bots use) -- this tells us whether the account itself is properly
deployed, connected to its broker, and synchronized.

Run with:
    python diagnose_account_state.py
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

    print("Fetching account details directly from MetaApi's account management API ...\n")
    account = await api.metatrader_account_api.get_account(account_id=METAAPI_ACCOUNT_ID)

    print("=" * 70)
    print("ACCOUNT STATE")
    print("=" * 70)
    print(f"  Account ID:        {account.id}")
    print(f"  Name:               {getattr(account, 'name', 'N/A')}")
    print(f"  Login:              {getattr(account, 'login', 'N/A')}")
    print(f"  Server:             {getattr(account, 'server', 'N/A')}")
    print(f"  State:              {getattr(account, 'state', 'N/A')}")
    print(f"  Connection status:  {getattr(account, 'connection_status', 'N/A')}")
    print(f"  Type:               {getattr(account, 'type', 'N/A')}")
    print(f"  Region:             {getattr(account, 'region', 'N/A')}")
    print(f"  Reliability:        {getattr(account, 'reliability', 'N/A')}")

    print(
        "\nWhat to look for:\n"
        "  - State should be 'DEPLOYED' (not 'UNDEPLOYED' or 'DEPLOYING')\n"
        "  - Connection status should be 'CONNECTED' (not 'DISCONNECTED' or\n"
        "    'DISCONNECTED_FROM_BROKER')\n"
        "  If either shows a problem, that's likely the REAL root cause -- not a\n"
        "  network issue on the server, and not something our bot code controls."
    )

    print("\n" + "=" * 70)
    print("RAW ATTRIBUTE DUMP (in case the above fields show 'N/A' -- the exact")
    print("attribute names may differ slightly from what's guessed above)")
    print("=" * 70)
    for attr_name in sorted(dir(account)):
        if attr_name.startswith("_"):
            continue
        try:
            value = getattr(account, attr_name)
            if callable(value):
                continue
            print(f"  {attr_name}: {value}")
        except Exception:  # noqa: BLE001
            continue


if __name__ == "__main__":
    asyncio.run(main())
