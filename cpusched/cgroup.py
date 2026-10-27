"""Read-only access to cgroup v2 CPU accounting and /proc counters.

Everything here is a plain file read; nothing is written. Functions take optional root/procfs
arguments so the tests can point them at a fake tree.
"""
from __future__ import annotations

import os
from pathlib import Path

CGROOT = Path("/sys/fs/cgroup")
PROCFS = Path("/proc")


def nproc() -> int:
    """CPUs on the machine (not this process's affinity mask: other users can use all of them)."""
    return os.cpu_count() or 1


def user_slice(uid: int, root: Path = CGROOT) -> Path:
    return root / "user.slice" / f"user-{uid}.slice"


def list_user_slices(root: Path = CGROOT) -> dict[int, Path]:
    """uid -> slice directory for every user slice systemd currently has on this machine."""
    out: dict[int, Path] = {}
    try:
        entries = list((root / "user.slice").iterdir())
    except OSError:
        return out
    for p in entries:
        name = p.name
        if name.startswith("user-") and name.endswith(".slice"):
            mid = name[len("user-"):-len(".slice")]
            if mid.isdigit():
                out[int(mid)] = p
    return out


def read_cpu_stat(cg: Path) -> dict[str, int]:
    """Parse <cg>/cpu.stat into ints; a missing or unreadable file gives {} (the cgroup may be gone)."""
    try:
        text = (cg / "cpu.stat").read_text()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, val = line.partition(" ")
        val = val.strip()
        if key and val.lstrip("-").isdigit():
            out[key] = int(val)
    return out


def read_cpu_max(cg: Path) -> tuple[float | None, int] | None:
    """<cg>/cpu.max as (cores or None for 'max', period_usec); None if unreadable."""
    try:
        parts = (cg / "cpu.max").read_text().split()
    except OSError:
        return None
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    quota, period = parts[0], int(parts[1])
    if quota == "max":
        return None, period
    return int(quota) / period, period


def read_proc_stat(procfs: Path = PROCFS) -> tuple[int, int]:
    """(busy, total) jiffies summed over all CPUs, from the aggregate 'cpu' line of /proc/stat.

    busy = user + nice + system + irq + softirq + steal; idle = idle + iowait.
    guest and guest_nice are already included in user and nice.
    """
    with open(procfs / "stat") as f:
        first = f.readline().split()
    if not first or first[0] != "cpu":
        raise RuntimeError("unexpected /proc/stat format")
    vals = [int(x) for x in first[1:]]
    vals += [0] * (10 - len(vals))
    user, nice, system, idle, iowait, irq, softirq, steal = vals[:8]
    busy = user + nice + system + irq + softirq + steal
    return busy, busy + idle + iowait


def read_procs_running(procfs: Path = PROCFS) -> int:
    """Runnable tasks right now (the instantaneous run-queue length, incl. running)."""
    try:
        with open(procfs / "stat") as f:
            for line in f:
                if line.startswith("procs_running"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def read_load1(procfs: Path = PROCFS) -> float:
    try:
        return float((procfs / "loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
