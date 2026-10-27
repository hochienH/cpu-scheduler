"""Pin every task of the job to the K CPUs it is allowed.

A CPUQuota alone spreads N workers thinly over the whole machine; with N >> K the kernel throttles them
in short bursts on many CPUs and the frequency governor never ramps those CPUs up, which cost 2.5-5x
throughput in bench/ (32 workers at a 2-core cap: quota only +248%, affinity only +12%, both -1%).
So the controller also sets the affinity of all tasks in the scope to K CPUs on distinct physical
cores, chosen among the idlest, and keeps the current ones when the quota changes.
"""
from __future__ import annotations

import os
from pathlib import Path

from .cgroup import PROCFS

SYSCPU = Path("/sys/devices/system/cpu")


def topology(sys_cpu: Path = SYSCPU, ncpu: int | None = None) -> dict[int, tuple[int, int]]:
    """cpu -> (package, core) so SMT siblings can be told apart; a CPU without topology is its own core."""
    n = ncpu or os.cpu_count() or 1
    out: dict[int, tuple[int, int]] = {}
    for c in range(n):
        try:
            core = int((sys_cpu / f"cpu{c}" / "topology" / "core_id").read_text())
            pkg = int((sys_cpu / f"cpu{c}" / "topology" / "physical_package_id").read_text())
            out[c] = (pkg, core)
        except (OSError, ValueError):
            out[c] = (-1, c)
    return out


def per_cpu_jiffies(procfs: Path = PROCFS) -> dict[int, tuple[int, int]]:
    """cpu -> (busy, total) jiffies from the cpuN lines of /proc/stat."""
    out: dict[int, tuple[int, int]] = {}
    with open(procfs / "stat") as f:
        for line in f:
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            parts = line.split()
            vals = [int(x) for x in parts[1:]] + [0] * 10
            user, nice, system, idle, iowait, irq, softirq, steal = vals[:8]
            busy = user + nice + system + irq + softirq + steal
            out[int(parts[0][3:])] = (busy, busy + idle + iowait)
    return out


class PerCpuMeter:
    """Busy fraction of every CPU over the time since the previous call."""

    def __init__(self, procfs: Path = PROCFS) -> None:
        self.procfs = procfs
        self.prev = per_cpu_jiffies(procfs)

    def update(self) -> dict[int, float]:
        cur = per_cpu_jiffies(self.procfs)
        out: dict[int, float] = {}
        for c, (busy, total) in cur.items():
            pb, pt = self.prev.get(c, (busy, total))
            out[c] = (busy - pb) / (total - pt) if total > pt else 0.0
        self.prev = cur
        return out


def choose(k: int, current: list[int], busy: dict[int, float], topo: dict[int, tuple[int, int]]) -> list[int]:
    """k CPUs: keep the current ones (cache warmth), drop the busiest when shrinking, and when growing
    add CPUs from physical cores not yet in the set, idlest core first (a busy SMT sibling halves a
    CPU's throughput, so a core's load is the sum over its threads)."""
    cur = [c for c in current if c in topo]
    if len(cur) >= k:
        cur.sort(key=lambda c: (busy.get(c, 0.0), c))
        return sorted(cur[:k])
    core_load: dict[tuple[int, int], float] = {}
    for c, core in topo.items():
        core_load[core] = core_load.get(core, 0.0) + busy.get(c, 0.0)
    chosen = list(cur)
    used = {topo[c] for c in chosen}
    remaining = [c for c in topo if c not in chosen]
    while len(chosen) < k and remaining:
        c = min(remaining, key=lambda x: (topo[x] in used, round(core_load[topo[x]], 2),
                                          round(busy.get(x, 0.0), 2), x))
        chosen.append(c)
        used.add(topo[c])
        remaining.remove(c)
    return sorted(chosen)


def scope_tasks(scope_path: Path, procfs: Path = PROCFS) -> list[int]:
    """Thread ids of every task in the scope: cgroup.threads, or cgroup.procs expanded via /proc."""
    try:
        return [int(x) for x in (scope_path / "cgroup.threads").read_text().split()]
    except OSError:
        pass
    try:
        pids = [int(x) for x in (scope_path / "cgroup.procs").read_text().split()]
    except OSError:
        return []
    tids: list[int] = []
    for pid in pids:
        try:
            tids += [int(t) for t in os.listdir(procfs / str(pid) / "task")]
        except OSError:
            tids.append(pid)
    return tids


def pin(tids: list[int], cpus: list[int]) -> int:
    """sched_setaffinity on each task; tasks that vanished meanwhile are skipped. Returns how many were set."""
    n = 0
    for tid in tids:
        try:
            os.sched_setaffinity(tid, cpus)
            n += 1
        except OSError:
            pass
    return n


def fmt_cpus(cpus: list[int]) -> str:
    """0,1,2,5,6 -> '0-2,5-6'."""
    if not cpus:
        return ""
    parts, start, prev = [], cpus[0], cpus[0]
    for c in cpus[1:]:
        if c == prev + 1:
            prev = c
            continue
        parts.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = c
    parts.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(parts)
