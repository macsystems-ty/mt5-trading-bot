"""
analyze_staleness_warnings.py

Extracts every "STALE PRICE DETECTED" warning from the bot's log
files and reports the real distribution of staleness values, PLUS
checks for time-based clustering -- both by hour of day, and whether
multiple bots experience staleness gaps at the SAME real-world
moment (suggesting a shared cause: MetaApi infrastructure, account-
level rate limiting, or broker-side issues) versus independently
(suggesting separate, per-bot causes).

Run with:
    python analyze_staleness_warnings.py
(scans all known log locations by default, or pass file paths/globs)
"""

import glob
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

LOG_LINE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| WARNING \| STALE PRICE DETECTED:.*?is (\d+)s old"
)

DEFAULT_LOG_GLOBS = [
    os.path.expanduser("~/mt5-trading-bot/src/live/logs/*.log"),
    os.path.expanduser("~/mt5-trading-bot/live_bots/*/logs/*.log"),
]


def bot_name_from_path(path):
    if "src/live/logs" in path:
        return "1HZ25V (src/live)"
    parts = path.split(os.sep)
    for i, part in enumerate(parts):
        if part == "live_bots" and i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


def find_log_files(args):
    if args:
        files = []
        for pattern in args:
            files.extend(glob.glob(pattern))
        return files

    files = []
    for pattern in DEFAULT_LOG_GLOBS:
        files.extend(glob.glob(pattern))
    return files


def main() -> None:
    log_files = find_log_files(sys.argv[1:])

    if not log_files:
        print("No log files found. Pass file paths/globs as arguments, or check DEFAULT_LOG_GLOBS.")
        return

    print(f"Scanning {len(log_files)} log file(s) ...\n")

    all_staleness_values = []
    per_file_counts = {}
    all_events = []  # (timestamp, staleness_seconds, bot_name)

    for path in log_files:
        try:
            with open(path, "r", errors="ignore") as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not read {path}: {exc!r}")
            continue

        bot_name = bot_name_from_path(path)
        file_values = []
        for line in lines:
            m = LOG_LINE_PATTERN.match(line)
            if m:
                ts_str, staleness_str = m.groups()
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                staleness = int(staleness_str)
                file_values.append(staleness)
                all_events.append((ts, staleness, bot_name))

        if file_values:
            per_file_counts[path] = len(file_values)
            all_staleness_values.extend(file_values)

    if not all_staleness_values:
        print("No staleness warnings found in any log file.")
        return

    all_staleness_values.sort()
    n = len(all_staleness_values)

    print(f"Total staleness warnings found: {n}\n")

    print("Per-file counts:")
    for path, count in sorted(per_file_counts.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {count:>6}  {path}")
    if len(per_file_counts) > 15:
        print(f"  ... and {len(per_file_counts) - 15} more files")

    print(f"\n{'=' * 70}")
    print("DISTRIBUTION OF STALENESS VALUES (seconds)")
    print(f"{'=' * 70}")
    print(f"  Minimum:        {all_staleness_values[0]}s")
    print(f"  Maximum:        {all_staleness_values[-1]}s")
    print(f"  Median:         {all_staleness_values[n // 2]}s")
    print(f"  Average:        {sum(all_staleness_values) / n:.1f}s")

    buckets = [
        ("30-40s (likely just normal jitter)", 30, 40),
        ("40-60s (mild delay)", 40, 60),
        ("60-120s (1-2 min, noteworthy)", 60, 120),
        ("120-600s (2-10 min, real concern)", 120, 600),
        ("600s+ (10+ min, serious issue)", 600, float("inf")),
    ]

    print(f"\n  Breakdown by severity:")
    for label, lo, hi in buckets:
        count = sum(1 for v in all_staleness_values if lo <= v < hi)
        pct = count / n * 100
        print(f"    {label:>45}: {count:>6} ({pct:>5.1f}%)")

    # --- TIME-OF-DAY CLUSTERING ---
    by_hour = defaultdict(int)
    for ts, staleness, bot_name in all_events:
        by_hour[ts.hour] += 1

    print(f"\n{'=' * 70}")
    print("WARNINGS BY HOUR OF DAY (server local/UTC time, whichever logs use)")
    print(f"{'=' * 70}")
    max_count = max(by_hour.values()) if by_hour else 1
    for hour in range(24):
        count = by_hour.get(hour, 0)
        bar = "#" * int(count / max_count * 40) if max_count else ""
        print(f"  {hour:>2}:00 | {count:>5} {bar}")

    # --- CROSS-BOT CORRELATION ---
    # Group events into 5-minute buckets; for each bucket, count how
    # many DISTINCT bots had a warning in that bucket. If most
    # multi-warning buckets involve MULTIPLE bots simultaneously,
    # that strongly suggests a SHARED cause (MetaApi infra, account-
    # level rate limit) rather than independent per-bot issues.
    bucket_to_bots = defaultdict(set)
    for ts, staleness, bot_name in all_events:
        bucket_key = ts.replace(minute=(ts.minute // 5) * 5, second=0)
        bucket_to_bots[bucket_key].add(bot_name)

    multi_bot_buckets = sum(1 for bots in bucket_to_bots.values() if len(bots) > 1)
    single_bot_buckets = sum(1 for bots in bucket_to_bots.values() if len(bots) == 1)
    total_buckets = len(bucket_to_bots)

    print(f"\n{'=' * 70}")
    print("CROSS-BOT CORRELATION (5-minute time buckets)")
    print(f"{'=' * 70}")
    print(f"  Total time buckets with at least one warning: {total_buckets}")
    print(
        f"  Buckets where MULTIPLE bots were affected simultaneously: "
        f"{multi_bot_buckets} ({multi_bot_buckets/total_buckets*100:.1f}%)"
    )
    print(
        f"  Buckets where only ONE bot was affected: "
        f"{single_bot_buckets} ({single_bot_buckets/total_buckets*100:.1f}%)"
    )

    print(
        "\nINTERPRETATION:\n"
        "  STALENESS SEVERITY: heavy weighting toward 2-10min or 10min+ means this is\n"
        "  NOT normal network jitter -- raising the 30s threshold would not meaningfully\n"
        "  help, since it wouldn't touch the dominant multi-minute cases.\n\n"
        "  HOUR CLUSTERING: if warnings concentrate in specific hours, that points to a\n"
        "  time-based cause (e.g. broker server maintenance windows, low-liquidity\n"
        "  periods, or scheduled MetaApi maintenance).\n\n"
        "  CROSS-BOT CORRELATION: if most multi-warning buckets show MULTIPLE bots\n"
        "  affected at the same time, this strongly suggests a SHARED cause (MetaApi\n"
        "  infrastructure load, or the account-level subscription limit we found\n"
        "  earlier) rather than 5 independent, unrelated problems."
    )


if __name__ == "__main__":
    main()