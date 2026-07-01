import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy"))

import backtester_trend_pullback_v2 as bt
import backtester_sr_patterns_combined as combined
import compare_level_age_caps as age_caps

candles_5min = bt.load_candles("5min")
trend_series = combined.build_trend_series_for_range(candles_5min)
trades = age_caps.simulate(candles_5min, trend_series, 200)
decided = [t for t in trades if t.pct_change is not None and t.initial_stop_distance_pct]

distances = sorted(t.initial_stop_distance_pct for t in decided)
n = len(distances)
print(f"Total trades: {n}")
print(f"Min: {distances[0]:.4f}%")
print(f"10th pct: {distances[int(n*0.10)]:.4f}%")
print(f"25th pct: {distances[int(n*0.25)]:.4f}%")
print(f"50th pct (median): {distances[int(n*0.50)]:.4f}%")
print(f"75th pct: {distances[int(n*0.75)]:.4f}%")
print(f"90th pct: {distances[int(n*0.90)]:.4f}%")
print(f"95th pct: {distances[int(n*0.95)]:.4f}%")
print(f"99th pct: {distances[int(n*0.99)]:.4f}%")
print(f"Max: {distances[-1]:.4f}%")
