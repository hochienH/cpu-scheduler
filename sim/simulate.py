"""Replay the cpusched policy on recorded traces to choose interval, margin and up_steps.

Input: CSVs written by `cpusched record` (1 Hz: others = cores used by everybody but me). The
simulated job wants `--demand` cores (default: the policy's max) on top of that trace.

For every (interval, margin, up_steps) the controller is replayed exactly as in production
(cpusched.policy.decide) on the windowed mean of `others`, and three things are scored per second:
  contention  core-seconds where our use + others > nproc (we slowed somebody down)
  got         core-seconds we obtained (min(our use, cores actually free))
  oracle      what a controller with instantaneous knowledge and the same min/max/margin would get
plus the number of quota changes. Efficiency = got / oracle.

Also prints the autocorrelation time of `others` and the distribution of its upward jumps over T
seconds, which is what makes a given interval safe or not on this machine.

    .venv/bin/python sim/simulate.py sim/traces/*.csv [--out sim/results.csv]
"""
from __future__ import annotations

import argparse
import csv
import itertools
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpusched.policy import PolicyConfig, PolicyState, decide, target  # noqa: E402


def load_trace(path: Path) -> tuple[list[float], int, str]:
    others, nproc, gaps = [], None, 0
    with open(path) as f:
        for row in csv.DictReader(f):
            dt = float(row["dt"])
            if dt > 3:
                gaps += 1
            others.append(float(row["others"]))
            nproc = int(row["nproc"])
    return others, nproc, f"{path.name}: {len(others)} s" + (f", {gaps} gaps" if gaps else "")


def simulate(others: list[float], nproc: int, cfg: PolicyConfig, interval: int, demand: int) -> dict:
    T = len(others)
    first = statistics.fmean(others[:interval]) if T >= interval else (others[0] if others else 0.0)
    state = PolicyState(quota=target(nproc, first, cfg))
    got = oracle = contention = 0.0
    changes = cont_s = 0
    quotas = []
    for t in range(T):
        if t and t % interval == 0:
            _, action = decide(nproc, statistics.fmean(others[t - interval:t]), cfg, state)
            changes += action in ("up", "down")
        q = state.quota
        use = min(q, demand)
        free = max(nproc - others[t], 0.0)
        got += min(use, free)
        over = use + others[t] - nproc
        if over > 0:
            contention += over
            cont_s += 1
        oracle += min(min(target(nproc, others[t], cfg), demand), free)
        quotas.append(q)
    hours = T / 3600 if T else 1
    return dict(got=got, oracle=oracle, eff=got / oracle if oracle else 1.0, contention=contention,
                cont_s=cont_s, cont_pct=100 * cont_s / T if T else 0, changes_h=changes / hours,
                mean_quota=statistics.fmean(quotas) if quotas else 0)


def autocorr_time(x: list[float], max_lag: int = 3600) -> int | None:
    n = len(x)
    if n < 10:
        return None
    mu = statistics.fmean(x)
    var = sum((v - mu) ** 2 for v in x) / n
    if var == 0:
        return 0
    for lag in range(1, min(max_lag, n // 2)):
        c = sum((x[i] - mu) * (x[i + lag] - mu) for i in range(n - lag)) / (n - lag) / var
        if c < 1 / math.e:
            return lag
    return max_lag


def jump_quantiles(x: list[float], T: int) -> tuple[float, float]:
    """95th and 99th percentile of others(t+T) - others(t): how much load can appear within one interval."""
    if len(x) <= T:
        return 0.0, 0.0
    d = sorted(x[i + T] - x[i] for i in range(len(x) - T))
    return d[int(0.95 * (len(d) - 1))], d[int(0.99 * (len(d) - 1))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+", type=Path)
    ap.add_argument("--intervals", default="5,10,20,30,60,120,300")
    ap.add_argument("--margins", default="2,4,8")
    ap.add_argument("--up-steps", default="1,2,3")
    ap.add_argument("--min", type=int, default=2)
    ap.add_argument("--max", type=int, help="default nproc // 2 of each trace")
    ap.add_argument("--demand", type=int, help="cores the simulated job wants (default: max)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    intervals = [int(v) for v in a.intervals.split(",")]
    margins = [float(v) for v in a.margins.split(",")]
    ups = [int(v) for v in a.up_steps.split(",")]

    out_rows = []
    for path in a.traces:
        others, nproc, desc = load_trace(path)
        if not others:
            print(f"{desc}: empty, skipped")
            continue
        mx = a.max or max(nproc // 2, 1)
        demand = a.demand or mx
        tau = autocorr_time(others)
        print(f"\n== {desc}, nproc {nproc}, others mean {statistics.fmean(others):.1f} "
              f"max {max(others):.1f}, autocorrelation time {tau} s")
        print("   T(s)  jump95  jump99   (cores of extra load that can appear within one interval)")
        for T in intervals:
            j95, j99 = jump_quantiles(others, T)
            print(f"   {T:>4}  {j95:6.1f}  {j99:6.1f}")
        print(f"\n   interval margin up_steps  eff%  contention(core-s/h)  cont%  changes/h  mean_quota")
        for T, m, u in itertools.product(intervals, margins, ups):
            cfg = PolicyConfig(min_cores=a.min, max_cores=mx, margin=m, up_steps=u)
            r = simulate(others, nproc, cfg, T, demand)
            hours = len(others) / 3600
            print(f"   {T:>8} {m:>6g} {u:>8}  {100 * r['eff']:5.1f}  {r['contention'] / hours:19.1f}  "
                  f"{r['cont_pct']:5.1f}  {r['changes_h']:9.1f}  {r['mean_quota']:10.1f}")
            out_rows.append(dict(trace=path.name, nproc=nproc, interval=T, margin=m, up_steps=u, **r))
    if a.out and out_rows:
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
            w.writeheader()
            w.writerows(out_rows)
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
