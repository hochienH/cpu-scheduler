"""`cpusched run`: launch a command in a scope and steer its CPUQuota from the load other users generate,
pinning the scope's tasks to as many CPUs as the quota allows.

Also `watch` (the same loop without a job, printing what it would do) and `status`.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import affinity as af
from . import cgroup as cg
from . import systemd
from .policy import PolicyConfig, PolicyState, decide, target
from .sampler import Load, LoadSampler

LOG_FIELDS = ["time", "dt", "nproc", "total_busy", "mine", "others", "system", "procs_running", "load1",
              "share", "target", "quota", "action", "cpus", "pinned", "scope_cores", "scope_throttled",
              "scope_nr_throttled", "per_uid"]


def default_log_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "cpusched"


def fmt_per_uid(per_uid: dict[int, float], me: int) -> str:
    items = sorted(((u, c) for u, c in per_uid.items() if c >= 0.05), key=lambda x: -x[1])
    return ";".join(f"{'me' if u == me else u}:{c:.1f}" for u, c in items)


def _f(x: float | None, nd: int = 2) -> str:
    return "" if x is None else f"{x:.{nd}f}"


def log_row(load: Load, cfg: PolicyConfig, tgt: int, quota: int, action: str, me: int,
            cpus: list[int] | None = None, pinned: int | None = None) -> dict[str, str]:
    return {
        "time": dt.datetime.now().isoformat(timespec="seconds"), "dt": _f(load.dt, 1),
        "nproc": str(load.nproc), "total_busy": _f(load.total_busy), "mine": _f(load.mine),
        "others": _f(load.others), "system": _f(load.system), "procs_running": str(load.procs_running),
        "load1": _f(load.load1), "share": str(cfg.share), "target": str(tgt), "quota": str(quota),
        "action": action, "cpus": af.fmt_cpus(cpus) if cpus else "",
        "pinned": "" if pinned is None else str(pinned),
        "scope_cores": _f(load.scope), "scope_throttled": _f(load.scope_throttled, 3),
        "scope_nr_throttled": "" if load.scope_nr_throttled is None else str(load.scope_nr_throttled),
        "per_uid": fmt_per_uid(load.per_uid, me),
    }


def say(msg: str) -> None:
    print(f"[cpusched] {msg}", file=sys.stderr, flush=True)


def describe(load: Load, cfg: PolicyConfig, tgt: int, quota: int, action: str,
             cpus: list[int] | None = None) -> str:
    free = load.nproc - load.total_busy
    s = (f"others={load.others:.1f} mine={load.mine:.1f} free={free:.1f}/{load.nproc} "
         f"share={cfg.share} target={tgt} quota={quota} {action}")
    return s + (f" cpus={af.fmt_cpus(cpus)}" if cpus else "")


def _wait(proc: subprocess.Popen, seconds: float) -> bool:
    """True if the process exited within `seconds`."""
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def run(cmd: list[str], cfg: PolicyConfig, interval: float, label: str | None = None,
        log_path: Path | None = None, verbose: bool = False, presample: float = 2.0,
        pin: bool = True) -> int:
    """Run `cmd` under a steered CPUQuota; return its exit status (128+signal if it was killed).

    With `pin` (default) every task in the scope is also bound to `quota` CPUs chosen on distinct,
    idle physical cores: a quota alone lets N workers smear over the whole machine, where CFS
    throttling and frequency scaling cost up to 5x throughput (see bench/).
    """
    reason = systemd.available()
    if reason:
        raise SystemExit(f"cpusched: {reason}")
    if not systemd.cpu_delegated():
        raise SystemExit("cpusched: the cpu controller is not delegated to your user slice; "
                         "CPUQuota cannot be set without root on this machine")
    sampler = LoadSampler()
    me = sampler.uid
    meter = af.PerCpuMeter() if pin else None
    topo = af.topology(ncpu=sampler.ncpu) if pin else {}

    first = sampler.snapshot()
    time.sleep(presample)
    load = sampler.diff(first, sampler.snapshot())
    busy = meter.update() if meter else {}
    try:
        cfg.share = len(systemd.list_scopes()) + 1
    except systemd.SystemdError as e:
        say(f"warning: {e}")
    state = PolicyState(quota=target(load.nproc, load.others, cfg))
    cpus: list[int] = af.choose(state.quota, [], busy, topo) if pin else []

    unit = systemd.unit_name(label or os.path.basename(cmd[0]))
    proc = systemd.launch_scope(unit, cmd, state.quota)
    # The pid survives systemd-run's exec and children inherit the mask at fork, so pinning the
    # main process now covers the workers it is about to start; later passes catch everything else.
    pinned = af.pin([proc.pid], cpus) if pin else None

    def forward(signum: int, _frame) -> None:
        name = signal.Signals(signum).name.removeprefix("SIG")
        try:
            systemd.kill_scope(unit, name)
        except systemd.SystemdError as e:
            say(f"could not forward SIG{name}: {e}")

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGHUP, forward)
    # Ctrl-C from the terminal already reaches the job (same foreground process group); if the wrapper
    # also forwarded it the job would see it twice, so the wrapper itself ignores SIGINT.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    for _ in range(50):                                   # the unit can take a moment to appear
        if proc.poll() is not None:
            break
        try:
            sampler.scope_path = systemd.control_group(unit)
        except systemd.SystemdError:
            sampler.scope_path = None
        if sampler.scope_path is not None:
            break
        time.sleep(0.1)

    def repin() -> int | None:
        if not pin:
            return None
        tasks = af.scope_tasks(sampler.scope_path) if sampler.scope_path else [proc.pid]
        return af.pin(tasks, cpus)

    log = writer = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "w", newline="")
        writer = csv.DictWriter(log, fieldnames=LOG_FIELDS)
        writer.writeheader()

    def emit(load: Load, tgt: int, action: str, pinned: int | None) -> None:
        if writer is not None:
            writer.writerow(log_row(load, cfg, tgt, state.quota, action, me, cpus, pinned))
            log.flush()
        if verbose:
            say(describe(load, cfg, tgt, state.quota, action, cpus))

    say(f"unit {unit}.scope pid {proc.pid} quota {state.quota} cores "
        f"(min {cfg.min_cores}, max {cfg.max_cores}, margin {cfg.margin:g}, every {interval:g}s"
        + (f", cpus {af.fmt_cpus(cpus)}" if pin else ", no pinning") + ")"
        + (f"; log {log_path}" if log_path else ""))
    emit(load, state.quota, "start", pinned)

    prev = sampler.snapshot()
    try:
        while not _wait(proc, interval):
            cur = sampler.snapshot()
            load = sampler.diff(prev, cur)
            prev = cur
            busy = meter.update() if meter else {}
            try:
                cfg.share = max(len(systemd.list_scopes()), 1)
            except systemd.SystemdError as e:
                say(f"warning: {e}")
            tgt = target(load.nproc, load.others, cfg)
            quota, action = decide(load.nproc, load.others, cfg, state)
            if action in ("up", "down"):
                try:
                    systemd.set_quota(unit, quota)
                except systemd.SystemdError as e:
                    if proc.poll() is None:
                        say(f"set-property failed: {e}")
                    action += "-failed"
                if pin:
                    cpus = af.choose(quota, cpus, busy, topo)
            emit(load, tgt, action, repin())
    finally:
        if log is not None:
            log.close()

    rc = proc.returncode
    if rc < 0:
        say(f"job killed by signal {-rc}")
        return 128 - rc
    return rc


def watch(cfg: PolicyConfig, interval: float, count: int | None = None) -> int:
    """Dry run: sample like `run` would and print the decision, without starting anything."""
    sampler = LoadSampler()
    meter = af.PerCpuMeter()
    topo = af.topology(ncpu=sampler.ncpu)
    prev = sampler.snapshot()
    time.sleep(min(interval, 2.0))
    load = sampler.diff(prev, sampler.snapshot())
    state = PolicyState(quota=target(load.nproc, load.others, cfg))
    cpus = af.choose(state.quota, [], meter.update(), topo)
    say(describe(load, cfg, state.quota, state.quota, "start", cpus))
    prev = sampler.snapshot()
    n = 0
    while count is None or n < count:
        time.sleep(interval)
        cur = sampler.snapshot()
        load = sampler.diff(prev, cur)
        prev = cur
        busy = meter.update()
        tgt = target(load.nproc, load.others, cfg)
        quota, action = decide(load.nproc, load.others, cfg, state)
        if action in ("up", "down"):
            cpus = af.choose(quota, cpus, busy, topo)
        say(describe(load, cfg, tgt, quota, action, cpus) + "  " + fmt_per_uid(load.per_uid, sampler.uid))
        n += 1
    return 0


def status() -> int:
    """List my cpusched scopes on this machine with their current cap, CPU set and usage."""
    reason = systemd.available()
    if reason:
        raise SystemExit(f"cpusched: {reason}")
    units = systemd.list_scopes()
    if not units:
        print("no cpusched scopes running")
        return 0
    print(f"{'unit':50} {'cap':>5} {'cpus':16} {'used_s':>8} {'thr_s':>7} {'nr_thr':>7}")
    for u in units:
        try:
            path = systemd.control_group(u)
        except systemd.SystemdError:
            path = None
        st = cg.read_cpu_stat(path) if path else {}
        cm = cg.read_cpu_max(path) if path else None
        cap = "max" if cm is None or cm[0] is None else f"{cm[0]:g}"
        cpus = ""
        for tid in af.scope_tasks(path) if path else []:
            try:
                cpus = af.fmt_cpus(sorted(os.sched_getaffinity(tid)))
                break
            except OSError:
                continue
        print(f"{u:50} {cap:>5} {cpus:16} {st.get('usage_usec', 0) / 1e6:8.0f} "
              f"{st.get('throttled_usec', 0) / 1e6:7.0f} {st.get('nr_throttled', 0):7d}")
    return 0
