"""
diagnose_trade_frequency_drop.py

Diagnoses why real trade frequency may have dropped, checking TWO
genuinely different possible causes per market:

  1. TREND DISTRIBUTION: what fraction of recent heartbeats showed
     UP/DOWN (entries possible) vs FLAT (no entries possible at all)?
  2. FILTER-RELATED LOG HINTS: a check for whether filter rejections
     are even being logged explicitly (if not, that's a real gap to
     fix so this can be measured directly going forward).

Run this ON THE SERVER:
    python diagnose_trade_frequency_drop.py
"""

import glob
import os
import re
from collections import defaultdict

LIVE_BOTS_DIR = os.path.expanduser("~/mt5-trading-bot/live_bots")
SRC_LIVE_DIR = os.path.expanduser("~/mt5-trading-bot/src/live")

TREND_PATTERN = re.compile(r"trend=(\w+)")
TRADE_OPENED_PATTERN = re.compile(r"TRADE OPENED")
PATTERN_MATCHED_NO_TRADE_HINTS = ["skipping entry", "trend strength", "overextended"]


def find_most_recent_log(bot_dir):
    logs = glob.glob(os.path.join(bot_dir, "logs", "*.log"))
    if not logs:
        return None
    return max(logs, key=os.path.getmtime)


def analyze_log(path, bot_name):
    print(f"\n{'=' * 80}")
    print(f"BOT: {bot_name}  ({path})")
    print(f"{'=' * 80}")

    try:
        with open(path, "r", errors="ignore") as f:
            lines = f.readlines()
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not read log: {exc!r}")
        return

    trend_counts = defaultdict(int)
    trade_opened_count = 0
    filter_hint_count = 0
    total_heartbeats = 0

    for line in lines:
        m = TREND_PATTERN.search(line)
        if m and "Heartbeat" in line:
            total_heartbeats += 1
            trend_counts[m.group(1)] += 1
        if TRADE_OPENED_PATTERN.search(line):
            trade_opened_count += 1
        line_lower = line.lower()
        for hint in PATTERN_MATCHED_NO_TRADE_HINTS:
            if hint in line_lower:
                filter_hint_count += 1
                break

    print(f"  Total heartbeats in this file: {total_heartbeats}")
    print(f"  Trade opened events in this file: {trade_opened_count}")
    print(f"  Lines mentioning filter-related hints: {filter_hint_count}")
    print(f"\n  Trend distribution:")
    for trend, count in sorted(trend_counts.items(), key=lambda kv: -kv[1]):
        pct = count / total_heartbeats * 100 if total_heartbeats else 0
        print(f"    {trend:>6}: {count:>5} ({pct:>5.1f}%)")

    if total_heartbeats and trend_counts.get("FLAT", 0) / total_heartbeats > 0.8:
        print(
            "\n  >>> LIKELY CAUSE: trend is FLAT >80% of the time -- this alone "
            "explains low trade frequency, REGARDLESS of any entry filter, since "
            "no entry is even attempted outside UP/DOWN trend."
        )
    elif filter_hint_count == 0 and trend_counts.get("FLAT", 0) / max(total_heartbeats, 1) < 0.5:
        print(
            "\n  >>> Trend looks reasonably active (not mostly FLAT), and no explicit "
            "filter-rejection logging exists to check directly. The trend-strength "
            "filter's rejection rate is NOT currently logged -- this is a real gap; "
            "consider adding an explicit log line each time it rejects a candidate, "
            "so this can be measured directly rather than inferred."
        )


def main() -> None:
    print("Scanning each bot's most recent log file ...")

    src_live_log = find_most_recent_log(SRC_LIVE_DIR)
    if src_live_log:
        analyze_log(src_live_log, "1HZ25V (src/live)")

    if os.path.isdir(LIVE_BOTS_DIR):
        for bot_name in sorted(os.listdir(LIVE_BOTS_DIR)):
            bot_dir = os.path.join(LIVE_BOTS_DIR, bot_name)
            if not os.path.isdir(bot_dir):
                continue
            log_path = find_most_recent_log(bot_dir)
            if log_path:
                analyze_log(log_path, bot_name)


if __name__ == "__main__":
    main()
