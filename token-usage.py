#!/usr/bin/env python3
"""Show daily token usage, categorized by source."""
import os, sys
from collections import defaultdict

log_file = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TOKEN_USAGE_LOG")
if not log_file:
    sys.exit("Usage: token-usage.py <log_file>  or set TOKEN_USAGE_LOG env var")

try:
    with open(log_file) as f:
        lines = f.readlines()
except FileNotFoundError:
    print("No usage log yet.")
    sys.exit(0)

# Structure: days[date][source] = {in, cached, out, cost}
data = defaultdict(lambda: defaultdict(lambda: {"in": 0, "cached": 0, "out": 0, "cost": 0.0}))

for line in lines:
    try:
        parts = line.split("|")
        day = parts[0].strip()[:10]
        
        source = parts[2].strip()
        metrics_part = parts[3].strip()
        cost_part = parts[4].strip()
        
        # Parse metrics
        metrics = metrics_part.split()
        d = {m.split("=")[0]: int(m.split("=")[1]) for m in metrics if "=" in m}
        
        cost = float(cost_part.replace("$", ""))
        
        entry = data[day][source]
        entry["in"] += d.get("in", 0)
        entry["cached"] += d.get("cached", 0)
        entry["out"] += d.get("out", 0)
        entry["cost"] += cost
        
    except (IndexError, ValueError):
        continue

print(f"{'Date':<12} | {'Source':<10} | {'In':<8} | {'Cached':<8} | {'Out':<8} | {'Cost':<8}")
print("-" * 65)

for day in sorted(data.keys(), reverse=True):
    for source, metrics in data[day].items():
        print(f"{day:<12} | {source:<10} | {metrics['in']:<8} | {metrics['cached']:<8} | {metrics['out']:<8} | ${metrics['cost']:<8.4f}")
