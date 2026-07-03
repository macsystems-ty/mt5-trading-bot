"""
analyze_recent_real_trades.py

Pulls REAL trade history directly from MetaApi (the broker's own
authoritative deal records, not reconstructed from logs) for the
last N days, and produces a clear summary broken down by market.

Run with:
    python src/backtest/analyze_recent_real_trades.py
"""

import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

load_dotenv()

METAAPI_TOKEN = os.getenv("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")

DAYS_TO_LOOK_BACK = 6  # covers the full period since the server went live (~June 24-25)
TOP_N_LARGEST_LOSSES = 10


def parse_deal_time(deal: dict):
    time_val = deal.get("time")
    if time_val is None:
        return None
    if isinstance(time_val, datetime):
        return time_val if time_val.tzinfo else time_val.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(time_val).replace("Z", "+00:00"))
    except ValueError:
        return None


async def main() -> None:
    if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
        print("ERROR: METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set in your .env file.")
        return

    api = MetaApi(token=METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(account_id=METAAPI_ACCOUNT_ID)
    connection = account.get_streaming_connection()

    print("Connecting and synchronizing (this can take a moment) ...")
    await connection.connect()
    await connection.wait_synchronized()

    history_storage = connection.history_storage
    deals = history_storage.deals

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=DAYS_TO_LOOK_BACK)

    print(f"\nTotal deals in local history storage: {len(deals)}")
    print(f"Filtering to: {start_time} -> {end_time}\n")

    recent_closing_deals_raw = [
        d for d in deals
        if d.get("entryType") == "DEAL_ENTRY_OUT"
        and parse_deal_time(d) is not None
        and start_time <= parse_deal_time(d) <= end_time
    ]

    # CRITICAL DEDUPLICATION: the bot maintains two parallel regional
    # connections for redundancy (london:0 and london:1) -- it's
    # possible for the SAME genuine broker-side deal to be reported
    # via both, resulting in duplicate entries in the LOCAL
    # history_storage.deals list, even though only ONE real trade
    # happened on the broker's side (confirmed directly against the
    # user's real MT5 terminal, which showed only one occurrence of
    # a trade that appeared TWICE in our unfiltered results). We
    # deduplicate by the deal's own unique 'id' field, which MetaApi
    # assigns per genuine broker-side deal.
    seen_ids = set()
    recent_closing_deals = []
    duplicate_count = 0
    for d in recent_closing_deals_raw:
        deal_id = d.get("id")
        if deal_id is not None and deal_id in seen_ids:
            duplicate_count += 1
            continue
        if deal_id is not None:
            seen_ids.add(deal_id)
        recent_closing_deals.append(d)

    if duplicate_count > 0:
        print(f"NOTE: removed {duplicate_count} duplicate deal record(s) (same deal ID seen more than once).\n")

    if not recent_closing_deals:
        print("No closed trades found in this time range.")
        await connection.close()
        return

    by_symbol = defaultdict(list)
    for d in recent_closing_deals:
        by_symbol[d.get("symbol", "UNKNOWN")].append(d)

    print("=" * 90)
    print(f"SUMMARY BY MARKET (last {DAYS_TO_LOOK_BACK} days, {len(recent_closing_deals)} closed trades total)")
    print("=" * 90)
    print(
        f"{'Symbol':>30} | {'Trades':>7} | {'Wins':>6} | {'Losses':>7} | "
        f"{'Win Rate':>9} | {'Total P&L':>12} | {'Avg/Trade':>10} | {'Profit Factor':>13}"
    )
    print("-" * 90)

    overall_pnl = 0.0
    market_summaries = []
    for symbol, symbol_deals in sorted(by_symbol.items(), key=lambda kv: -len(kv[1])):
        profits = [
            float(d.get("profit", 0)) + float(d.get("commission", 0)) + float(d.get("swap", 0))
            for d in symbol_deals
        ]
        wins = sum(1 for p in profits if p > 0)
        losses = sum(1 for p in profits if p <= 0)
        win_rate = wins / len(profits) * 100 if profits else 0
        total_pnl = sum(profits)
        overall_pnl += total_pnl
        avg_per_trade = total_pnl / len(profits) if profits else 0

        gross_profit = sum(p for p in profits if p > 0)
        gross_loss = abs(sum(p for p in profits if p <= 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        largest_win = max(profits) if profits else 0
        largest_loss = min(profits) if profits else 0

        market_summaries.append({
            "symbol": symbol, "trades": len(symbol_deals), "win_rate": win_rate,
            "total_pnl": total_pnl, "avg_per_trade": avg_per_trade, "profit_factor": profit_factor,
            "largest_win": largest_win, "largest_loss": largest_loss,
        })

        pf_display = f"{profit_factor:.2f}" if profit_factor != float("inf") else "inf (no losses)"
        print(
            f"{symbol:>30} | {len(symbol_deals):>7} | {wins:>6} | {losses:>7} | "
            f"{win_rate:>8.1f}% | ${total_pnl:>+10.2f} | ${avg_per_trade:>+8.2f} | {pf_display:>13}"
        )

    print(f"\n{'-' * 90}")
    print("LARGEST SINGLE WIN/LOSS PER MARKET (context: is total P&L driven by one outlier trade?)")
    print(f"{'-' * 90}")
    for m in market_summaries:
        print(
            f"  {m['symbol']:>30} | largest win: ${m['largest_win']:>+9.2f} | "
            f"largest loss: ${m['largest_loss']:>+9.2f}"
        )

    print(
        "\nNOTE on PROFIT FACTOR: gross profit / gross loss. Above 1.0 = profitable\n"
        "overall; higher is better. This is a more honest 'most profitable' signal\n"
        "than total P&L alone, since total P&L can be dominated by a single large\n"
        "win/loss rather than reflecting consistent, repeatable edge. A market with\n"
        "FEWER trades but a HIGHER profit factor may be more reliable than one with\n"
        "more trades but a lower, less consistent one."
    )
    print(f"{'TOTAL':>30} | {len(recent_closing_deals):>7} | {'':>6} | {'':>7} | {'':>9} | ${overall_pnl:>+10.2f}")

    print(f"\n{'=' * 90}")
    print(f"TOP {TOP_N_LARGEST_LOSSES} LARGEST LOSSES (across all markets)")
    print("=" * 90)

    all_with_pnl = [
        (d, float(d.get("profit", 0)) + float(d.get("commission", 0)) + float(d.get("swap", 0)))
        for d in recent_closing_deals
    ]
    all_with_pnl.sort(key=lambda x: x[1])

    print(f"{'Time':>26} | {'Symbol':>25} | {'Type':>6} | {'Volume':>8} | {'P&L':>10}")
    print("-" * 90)
    for d, pnl in all_with_pnl[:TOP_N_LARGEST_LOSSES]:
        print(
            f"{d.get('brokerTime', d.get('time', '')):>26} | {d.get('symbol', ''):>25} | "
            f"{d.get('type', ''):>6} | {d.get('volume', ''):>8} | ${pnl:>+8.2f}"
        )

    await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
