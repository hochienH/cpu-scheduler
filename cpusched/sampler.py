"""Turn two snapshots of the counters into 'how many cores is everybody using'.

`others` (what the policy acts on) is machine-wide busy cores from /proc/stat minus the usage of my
own user slice (all my processes, all sessions, all cpusched instances). The per-user breakdown from
the other user slices is informational: it says who is using the machine, and the modelling uses it.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cgroup as cg


@dataclass
class Snapshot:
    t: float                        # time.monotonic()
    wall: float                     # time.time()
    busy_j: int                     # /proc/stat busy jiffies
    total_j: int                    # /proc/stat total jiffies
    slices: dict[int, int]          # uid -> usage_usec of user-<uid>.slice
    system_usec: int | None         # system.slice usage_usec
    procs_running: int
    load1: float
    scope_usec: int | None = None   # the controlled scope (cpusched run only)
    scope_throttled_usec: int | None = None
    scope_nr_throttled: int | None = None


@dataclass
class Load:
    dt: float
    nproc: int
    total_busy: float               # cores, whole machine (/proc/stat)
    mine: float                     # cores, my user slice
    others: float                   # cores, everything that is not my slice (>= 0)
    system: float                   # cores, system.slice (part of `others`)
    per_uid: dict[int, float] = field(default_factory=dict)   # cores per user slice, mine included
    procs_running: int = 0
    load1: float = 0.0
    scope: float | None = None              # cores used by the controlled scope
    scope_throttled: float | None = None    # throttled seconds per wall second (0..1 per core-ish)
    scope_nr_throttled: int | None = None   # throttled CFS periods in the interval


class LoadSampler:
    def __init__(self, root: Path = cg.CGROOT, procfs: Path = cg.PROCFS, uid: int | None = None,
                 ncpu: int | None = None, scope_path: Path | None = None) -> None:
        self.root = Path(root)
        self.procfs = Path(procfs)
        self.uid = os.getuid() if uid is None else uid
        self.ncpu = cg.nproc() if ncpu is None else ncpu
        self.scope_path = scope_path

    def snapshot(self) -> Snapshot:
        busy, total = cg.read_proc_stat(self.procfs)
        slices: dict[int, int] = {}
        for uid, path in cg.list_user_slices(self.root).items():
            st = cg.read_cpu_stat(path)
            if "usage_usec" in st:
                slices[uid] = st["usage_usec"]
        system = cg.read_cpu_stat(self.root / "system.slice").get("usage_usec")
        snap = Snapshot(t=time.monotonic(), wall=time.time(), busy_j=busy, total_j=total,
                        slices=slices, system_usec=system,
                        procs_running=cg.read_procs_running(self.procfs),
                        load1=cg.read_load1(self.procfs))
        if self.scope_path is not None:
            st = cg.read_cpu_stat(self.scope_path)
            snap.scope_usec = st.get("usage_usec")
            snap.scope_throttled_usec = st.get("throttled_usec")
            snap.scope_nr_throttled = st.get("nr_throttled")
        return snap

    def diff(self, a: Snapshot, b: Snapshot) -> Load:
        dt = max(b.t - a.t, 1e-6)
        dj = b.total_j - a.total_j
        total_busy = (b.busy_j - a.busy_j) / dj * self.ncpu if dj > 0 else 0.0
        per_uid: dict[int, float] = {}
        for uid, usec in b.slices.items():
            if uid in a.slices:                      # a slice that appeared mid-interval is skipped once
                per_uid[uid] = max(usec - a.slices[uid], 0) / 1e6 / dt
        mine = per_uid.get(self.uid, 0.0)
        load = Load(dt=dt, nproc=self.ncpu, total_busy=total_busy, mine=mine,
                    others=max(total_busy - mine, 0.0),
                    system=_rate(a.system_usec, b.system_usec, dt) or 0.0,
                    per_uid=per_uid, procs_running=b.procs_running, load1=b.load1)
        if b.scope_usec is not None:
            load.scope = _rate(a.scope_usec, b.scope_usec, dt)
            load.scope_throttled = _rate(a.scope_throttled_usec, b.scope_throttled_usec, dt)
            if a.scope_nr_throttled is not None and b.scope_nr_throttled is not None:
                load.scope_nr_throttled = max(b.scope_nr_throttled - a.scope_nr_throttled, 0)
        return load


def _rate(a: int | None, b: int | None, dt: float) -> float | None:
    if a is None or b is None:
        return None
    return max(b - a, 0) / 1e6 / dt
