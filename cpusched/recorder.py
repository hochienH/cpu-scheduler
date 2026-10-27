"""`cpusched record`: write one CSV line per interval with machine-wide and per-user CPU usage.

Used to collect traces of what other users do over a day or two; the simulator in sim/ replays the
controller on those traces to choose the interval and the smoothing.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import time
from pathlib import Path

from .runner import fmt_per_uid
from .sampler import LoadSampler

FIELDS = ["time", "dt", "nproc", "total_busy", "mine", "managed", "others", "system", "procs_running", "load1",
          "per_uid"]


def record(out: Path, interval: float = 1.0, duration: float | None = None) -> int:
    sampler = LoadSampler()
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists() or out.stat().st_size == 0
    with open(out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        start = time.monotonic()
        next_t = start + interval
        prev = sampler.snapshot()
        while duration is None or time.monotonic() - start < duration:
            time.sleep(max(0.0, next_t - time.monotonic()))
            next_t += interval
            cur = sampler.snapshot()
            load = sampler.diff(prev, cur)
            prev = cur
            w.writerow({
                "time": dt.datetime.now().isoformat(timespec="seconds"), "dt": f"{load.dt:.2f}",
                "nproc": load.nproc, "total_busy": f"{load.total_busy:.2f}", "mine": f"{load.mine:.2f}",
                "managed": f"{load.managed:.2f}",
                "others": f"{load.others:.2f}", "system": f"{load.system:.2f}",
                "procs_running": load.procs_running, "load1": f"{load.load1:.2f}",
                "per_uid": fmt_per_uid(load.per_uid, sampler.uid),
            })
            f.flush()
    return 0
