"""Cost of CFS bandwidth throttling: N worker processes capped at K cores versus K workers uncapped.

Each condition runs the same CPU-bound Pool.map inside a transient systemd user scope with a fixed
CPUQuota (no controller involved), `--reps` times, conditions interleaved so slow drift in the
machine's load hits every condition alike. Writes a CSV and prints medians and overheads.

    .venv/bin/python bench/throttle_overhead.py --out bench/throttle_$(hostname).csv
"""
from __future__ import annotations

import argparse
import csv
import os
import platform
import statistics
import subprocess
import sys
import time

WORK = r"""
import multiprocessing as mp, sys, time
def chunk(i):
    s = 0
    for j in range(1_500_000):
        s += (j * j) % 7
    return s
if __name__ == "__main__":
    n, m = int(sys.argv[1]), int(sys.argv[2])
    t = time.perf_counter()
    with mp.Pool(n) as p:
        p.map(chunk, range(m), chunksize=1)
    print(f"{time.perf_counter() - t:.3f}")
"""


def run_once(workers: int, chunks: int, quota_cores: int | None, tag: str) -> float:
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", f"bench-{tag}-{os.getpid()}"]
    if quota_cores is not None:
        argv += ["-p", f"CPUQuota={quota_cores * 100}%"]
    argv += ["--", sys.executable, "-c", WORK, str(workers), str(chunks)]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip().splitlines()
    return float(out[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--caps", default="2,4,8", help="K values (cores)")
    ap.add_argument("--ratios", default="1,2,4,8,16", help="N/K values (workers per allowed core)")
    ap.add_argument("--chunks-per-core", type=int, default=24)
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    caps = [int(x) for x in a.caps.split(",")]
    ratios = [int(x) for x in a.ratios.split(",")]
    host = platform.node().lower()

    conds = []
    for k in caps:
        conds.append((k, k, None))                       # baseline: K workers, no cap
        for r in ratios:
            conds.append((k, k * r, k))                  # N = K*r workers under a K-core cap
    rows = []
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["host", "nproc", "cap", "workers", "rep", "wall_s"])
        for rep in range(a.reps):
            for i, (k, n, q) in enumerate(conds):
                wall = run_once(n, a.chunks_per_core * k, q, f"{rep}-{i}")
                rows.append((k, n, q, wall))
                w.writerow([host, os.cpu_count(), q if q is not None else "none", n, rep, f"{wall:.3f}"])
                f.flush()
                print(f"rep {rep} cap {q} workers {n}: {wall:.2f}s", flush=True)
                time.sleep(1)

    print("\ncap  workers  median_s  vs_baseline")
    for k in caps:
        base = statistics.median(wl for kk, n, q, wl in rows if kk == k and q is None)
        print(f"{k:>3}  {k:>7}  {base:8.2f}  baseline (uncapped)")
        for r in ratios:
            med = statistics.median(wl for kk, n, q, wl in rows if kk == k and q == k and n == k * r)
            print(f"{k:>3}  {k * r:>7}  {med:8.2f}  {100 * (med / base - 1):+.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
