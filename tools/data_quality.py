#!/usr/bin/env python3.11
"""Data-quality report for collected market data: coverage, gaps, latency, volume per market."""

from __future__ import annotations

import argparse
import collections
import statistics
from pathlib import Path

from mdlib import list_hours, read_records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/market")
    ap.add_argument("--gap-seconds", type=int, default=10)
    args = ap.parse_args()
    root = Path(args.root)
    files = list_hours(root)
    if not files:
        raise SystemExit("no data")
    n_ob = collections.Counter(); n_tr = collections.Counter()
    last_ob: dict[str, int] = {}
    gaps: dict[str, list[int]] = collections.defaultdict(list)
    lat: list[int] = []
    per_hour = collections.Counter()
    first = last = None
    for r in read_records(files):
        m, rx = r["m"], r["rx"]
        first = rx if first is None else min(first, rx); last = rx if last is None else max(last, rx)
        lat.append(rx - r["ts"])
        per_hour[rx // 3_600_000] += 1
        if r["t"] == "ob":
            n_ob[m] += 1
            prev = last_ob.get(m)
            if prev is not None and rx - prev > args.gap_seconds * 1000:
                gaps[m].append((rx - prev) // 1000)
            last_ob[m] = rx
        else:
            n_tr[m] += 1
    span_h = (last - first) / 3.6e6
    print(f"files={len(files)} span={span_h:.2f}h  records={sum(n_ob.values()) + sum(n_tr.values()):,}")
    lat.sort()
    print(f"rx - exchange_ts latency ms: p50={lat[len(lat)//2]} p95={lat[int(len(lat)*.95)]} p99={lat[int(len(lat)*.99)]}"
          "  (includes clock offset between this Mac and Upbit)")
    if len(per_hour) > 2:
        counts = [v for k, v in sorted(per_hour.items())][1:-1]
        print(f"records/hour (full hours): min={min(counts):,} median={int(statistics.median(counts)):,} max={max(counts):,}")
    print(f"\n{'market':<12}{'ob':>9}{'trades':>9}{'ob/s':>7}{'gaps>%ds':>10}{'maxgap_s':>10}" % args.gap_seconds)
    for m in sorted(n_ob, key=lambda k: -n_tr[k]):
        g = gaps.get(m, [])
        print(f"{m:<12}{n_ob[m]:>9,}{n_tr[m]:>9,}{n_ob[m] / max(span_h * 3600, 1):>7.2f}{len(g):>10}{(max(g) if g else 0):>10}")
    thin = [m for m in n_ob if n_tr[m] < span_h * 60]
    if thin:
        print(f"\nilliquid (<1 trade/min on average): {', '.join(m[4:] for m in thin)}")


if __name__ == "__main__":
    main()
