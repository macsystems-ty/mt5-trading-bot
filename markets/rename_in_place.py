"""
rename_in_place.py

For each market subfolder under markets/, renames any
"<SYMBOL>_backtest_strategy.py" or "<SYMBOL>_fetch_historical.py"
file already sitting inside it to the standardized filename
(backtest_strategy.py / fetch_historical.py).

Run this FROM INSIDE your markets/ folder:
    cd markets
    python rename_in_place.py
"""

import os
import re

MARKETS_DIR = os.path.dirname(os.path.abspath(__file__))

FILENAME_PATTERN = re.compile(r"^(.+?)_(backtest_strategy|fetch_historical)\.py$")


def main() -> None:
    renamed_count = 0
    skipped = []

    for entry in sorted(os.listdir(MARKETS_DIR)):
        market_dir = os.path.join(MARKETS_DIR, entry)
        if not os.path.isdir(market_dir):
            continue

        for filename in os.listdir(market_dir):
            match = FILENAME_PATTERN.match(filename)
            if not match:
                continue

            symbol, script_type = match.group(1), match.group(2)
            target_filename = f"{script_type}.py"
            source_path = os.path.join(market_dir, filename)
            target_path = os.path.join(market_dir, target_filename)

            if filename == target_filename:
                continue

            if os.path.exists(target_path):
                print(f"  SKIP (target already exists): {target_path}")
                skipped.append(source_path)
                continue

            os.rename(source_path, target_path)
            print(f"  Renamed {entry}/{filename} -> {entry}/{target_filename}")
            renamed_count += 1

        os.makedirs(os.path.join(market_dir, "data"), exist_ok=True)

    print(f"\nDone. Renamed {renamed_count} file(s).")
    if skipped:
        print(f"Skipped {len(skipped)} file(s) because a correctly-named file already existed:")
        for s in skipped:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
