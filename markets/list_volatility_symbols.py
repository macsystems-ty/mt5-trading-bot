"""
list_volatility_symbols.py

Fetches the DEFINITIVE, live list of active symbols directly from
Deriv's public API (active_symbols request), filtered to show all
Volatility Index variants -- both 1s and standard (2s).

Run with:
    python list_volatility_symbols.py
"""

import asyncio
import json

import websockets

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"


async def main() -> None:
    async with websockets.connect(DERIV_WS_URL) as ws:
        request = {"active_symbols": "brief", "product_type": "basic"}
        await ws.send(json.dumps(request))
        response_raw = await ws.recv()
        response = json.loads(response_raw)

        if "error" in response:
            print(f"ERROR from Deriv API: {response['error']}")
            return

        symbols = response.get("active_symbols", [])
        print(f"Total active symbols (all markets): {len(symbols)}\n")

        volatility_symbols = [
            s for s in symbols
            if "volatility" in s.get("market", "").lower()
            or "volatility" in s.get("submarket", "").lower()
            or "1HZ" in s.get("symbol", "")
            or s.get("symbol", "").startswith("R_")
        ]

        print(f"Volatility-related symbols found: {len(volatility_symbols)}\n")
        print(f"{'Symbol':<15} | {'Display Name':<35} | {'Market':<20} | {'Submarket'}")
        print("-" * 95)
        for s in sorted(volatility_symbols, key=lambda x: x.get("symbol", "")):
            print(
                f"{s.get('symbol', ''):<15} | {s.get('display_name', ''):<35} | "
                f"{s.get('market', ''):<20} | {s.get('submarket', '')}"
            )


if __name__ == "__main__":
    asyncio.run(main())
