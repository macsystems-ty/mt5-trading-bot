"""
inspect_specific_trade_entries.py

Looks up the real candle data and swing-level structure around 3
specific trade entry times, to check whether a Lower High formed
right before each BUY entry.

Tries the historical CSV first; if the entry time isn't covered
(e.g. it's more recent than the historical fetch), falls back to the
LIVE candle CSV (src/live/logs/<symbol>_5min_live.csv), which has
been accumulating real candles continuously since we added that
feature.

EDIT THE ENTRY_TIMES_TO_CHECK list below if needed before running.

Run with:
    python src/backtest/inspect_specific_trade_entries.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy")
)

import backtester_trend_pullback_v2 as bt  # noqa: E402
import backtester_sr_engulfing as sre  # noqa: E402
import backtester_sr_patterns_combined as combined  # noqa: E402

ENTRY_TIMES_TO_CHECK = [
    "2026-06-23 22:30:00",
    "2026-06-23 23:15:00",
    "2026-06-24 01:30:00",
]

CANDLES_BEFORE = 10
CANDLES_AFTER = 5

LIVE_BOT_SYMBOL_NAME = "Volatility 25 (1s) Index"  # must match SYMBOL in live_mt5_trading_bot.py exactly
LIVE_CSV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "live", "logs",
    f"{LIVE_BOT_SYMBOL_NAME.replace(' ', '_')}_5min_live.csv",
)


def load_live_candles():
    if not os.path.exists(LIVE_CSV_PATH):
        return None
    df = pd.read_csv(LIVE_CSV_PATH, parse_dates=["open_time"], index_col="open_time")
    df = df.sort_index()
    return df


def main() -> None:
    print(f"Loading 5min candles for {bt.SYMBOL} ...")
    historical_candles = bt.load_candles("5min")
    print(f"  Historical CSV: {len(historical_candles):,} candles, "
          f"range {historical_candles.index.min()} to {historical_candles.index.max()}")

    live_candles = load_live_candles()
    if live_candles is not None:
        print(f"  Live CSV found: {len(live_candles):,} candles, "
              f"range {live_candles.index.min()} to {live_candles.index.max()}\n")
    else:
        print(f"  Live CSV not found at {LIVE_CSV_PATH}\n")

    for entry_time_str in ENTRY_TIMES_TO_CHECK:
        entry_time = pd.Timestamp(entry_time_str, tz="UTC")

        print("=" * 75)
        print(f"ENTRY TIME: {entry_time}")
        print("=" * 75)

        if entry_time in historical_candles.index:
            candles_5min = historical_candles
            source = "historical CSV"
        elif live_candles is not None and entry_time in live_candles.index:
            candles_5min = live_candles
            source = "LIVE CSV"
        else:
            print(
                f"  NOT FOUND in either the historical CSV or the live CSV.\n"
                f"  Double-check the entry time matches your log exactly, or that\n"
                f"  the live CSV has data covering this period.\n"
            )
            continue

        print(f"  (using {source})")
        all_levels = sre.identify_swing_levels(candles_5min, combined.SWING_LOOKBACK)
        entry_idx = candles_5min.index.get_loc(entry_time)

        start_idx = max(0, entry_idx - CANDLES_BEFORE)
        end_idx = min(len(candles_5min), entry_idx + CANDLES_AFTER + 1)

        print(f"\n  Candles around entry (entry candle marked with '>>>'):")
        for idx in range(start_idx, end_idx):
            row = candles_5min.iloc[idx]
            time = candles_5min.index[idx]
            color = "GREEN" if row["close"] > row["open"] else ("RED" if row["close"] < row["open"] else "FLAT")
            marker = " >>> ENTRY CANDLE" if idx == entry_idx else ""
            print(
                f"    {time} | O={row['open']:.2f} H={row['high']:.2f} "
                f"L={row['low']:.2f} C={row['close']:.2f} [{color}]{marker}"
            )

        known_resistances = [
            lvl for lvl in all_levels
            if lvl["type"] == "resistance" and lvl["index"] + combined.SWING_LOOKBACK <= entry_idx
        ]
        known_supports = [
            lvl for lvl in all_levels
            if lvl["type"] == "support" and lvl["index"] + combined.SWING_LOOKBACK <= entry_idx
        ]

        recent_resistances = sorted(known_resistances, key=lambda lvl: lvl["index"])[-3:]
        recent_supports = sorted(known_supports, key=lambda lvl: lvl["index"])[-3:]

        print(f"\n  Most recent 3 CONFIRMED resistance levels (highs) before this entry:")
        for lvl in recent_resistances:
            lvl_time = candles_5min.index[lvl["index"]]
            print(f"    {lvl_time} | price={lvl['price']:.2f}")
        if len(recent_resistances) >= 2:
            is_hh = recent_resistances[-1]["price"] > recent_resistances[-2]["price"]
            print(f"    -> Most recent high vs previous: {'HIGHER (HH)' if is_hh else 'LOWER (LH) -- structure break!'}")

        print(f"\n  Most recent 3 CONFIRMED support levels (lows) before this entry:")
        for lvl in recent_supports:
            lvl_time = candles_5min.index[lvl["index"]]
            print(f"    {lvl_time} | price={lvl['price']:.2f}")
        if len(recent_supports) >= 2:
            is_hl = recent_supports[-1]["price"] > recent_supports[-2]["price"]
            print(f"    -> Most recent low vs previous: {'HIGHER (HL)' if is_hl else 'LOWER (LL)'}")

        print()


if __name__ == "__main__":
    main()