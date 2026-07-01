"""
check_symbol_spec.py

Checks the symbol specification (min/max/step lot size, contract size,
etc.) and live price/spread for Volatility 25 (1s) Index on your MT5
account -- needed before we can build correct position-sizing logic,
since MT5 uses lot/volume sizing, fundamentally different from
Deriv's Multiplier stake+multiplier system we used before.

Run with:
    python src/live/check_symbol_spec.py
"""

import asyncio
import os

from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

load_dotenv()

METAAPI_TOKEN = os.getenv("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")
SYMBOL = "Volatility 25 (1s) Index"


async def main() -> None:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        print("ERROR: METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set in your .env file.")
        return

    api = MetaApi(token=METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    if account.state != "DEPLOYED":
        await account.deploy()

    await account.wait_connected()

    connection = account.get_streaming_connection()
    await connection.connect()
    await connection.wait_synchronized()

    terminal_state = connection.terminal_state

    print(f"Subscribing to market data for '{SYMBOL}' ...")
    await connection.subscribe_to_market_data(symbol=SYMBOL)

    await asyncio.sleep(3)

    print("\n--- SYMBOL SPECIFICATION ---")
    spec = terminal_state.specification(symbol=SYMBOL)
    if spec:
        for key in [
            "tickSize", "contractSize", "minVolume", "maxVolume", "volumeStep",
            "digits", "pipSize",
        ]:
            print(f"  {key}: {spec.get(key)}")
    else:
        print("  No specification returned -- check the symbol name is exactly correct.")

    print("\n--- LIVE PRICE ---")
    price = terminal_state.price(symbol=SYMBOL)
    if price:
        bid = price.get("bid")
        ask = price.get("ask")
        print(f"  Bid: {bid}")
        print(f"  Ask: {ask}")
        if bid and ask:
            spread = ask - bid
            spread_pct = spread / ((bid + ask) / 2) * 100
            print(f"  Spread: {spread:.5f} ({spread_pct:.4f}%)")
    else:
        print("  No price returned yet -- market data may need more time to arrive.")

    await connection.unsubscribe_from_market_data(symbol=SYMBOL)


if __name__ == "__main__":
    asyncio.run(main())
