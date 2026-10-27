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
from .policy import PolicyConfig, PolicyState, decide, is_saturated, target
from .sampler import Load, LoadSampler

LOG_FIELDS = ["time", "dt", "nproc", "total_busy", "mine", "managed", "others", "system", "procs_running",
              "load1", "share", "target", "quota", "cap", "action", "cpus", "pinned", "scope_cores",
              "scope_throttled", "scope_nr_throttled", "per_uid"]


def default_log_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "cpusched"


def fmt_per_uid(per_uid: dict[int, float], me: int) -> str:
    items = sorted(((u, c) for u, c in per_uid.items() if c >= 0.05), key=lambda x: -x[1])
    return ";".join(f"{'me' if u == me else u}:{c:.1f}" for u, c in items)


def _f(x: float | None, nd: int = 2) -> str:
    return "" if x is None else f"{x:.{nd}f}"


def log_row(load: Load, cfg: PolicyConfig, tgt: int, quota: int, action: str, me: int,
            cpus: list[int] | None = None, pinned: int | None = None,
            cap: float | None = None) -> dict[str, str]:
    return {
        "time": dt.datetime.now().isoformat(timespec="seconds"), "dt": _f(load.dt, 1),
        "nproc": str(load.nproc), "total_busy": _f(load.total_busy), "mine": _f(load.mine),
        "managed": _f(load.managed), "others": _f(load.others), "system": _f(load.system),
        "procs_running": str(load.procs_running), "load1": _f(load.load1), "share": str(cfg.share),
        "target": str(tgt), "quota": str(quota), "cap": "" if cap is None else f"{cap:g}",
        "action": action, "cpus": af.fmt_cpus(cpus) if cpus else "",
        "pinned": "" if pinned is None else str(pinned),
        "scope_cores": _f(load.scope), "scope_throttled": _f(load.scope_throttled, 3),
        "scope_nr_throttled": "" if load.scope_nr_throttled is None else str(load.scope_nr_throttled),
        "per_uid": fmt_per_uid(load.per_uid, me),
    }


def say(msg: str) -> None:
    try:
        print(f"[cpusched] {msg}", file=sys.stderr, flush=True)
    except OSError:                                   # stderr closed (a dead pipe): keep running silently
        pass


def describe(load: Load, cfg: PolicyConfig, tgt: int, quota: int, action: str,
             cpus: list[int] | None = None, cap: float | None = None) -> str:
    free = load.nproc - load.total_busy
    s = (f"others={load.others:.1f} managed={load.managed:.1f} free={free:.1f}/{load.nproc} "
         f"share={cfg.share} target={tgt} quota={quota} {action}")
    if cap is not None and cap != quota:
        s += f" cap={cap:g}"
    return s + (f" cpus={af.fmt_cpus(cpus)}" if cpus else "")


class CsvLog:
    """The decision log. Opened before the job starts so a bad path fails first; a write error later
    disables the log instead of ending the controller."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.file = self.writer = None
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.file = open(path, "w", newline="")
            self.writer = csv.DictWriter(self.file, fieldnames=LOG_FIELDS)
            self.writer.writeheader()
            self.file.flush()
        except OSError as e:
            raise SystemExit(f"cpusched: cannot write the log {path}: {e} (use --log PATH or --no-log)")

    def write(self, row: dict[str, str]) -> None:
        if self.writer is None:
            return
        try:
            self.writer.writerow(row)
            self.file.flush()
        except OSError as e:
            say(f"log write failed, logging stops but the controller continues: {e}")
            self.close()

    def close(self) -> None:
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
        self.file = self.writer = None

    def discard(self) -> None:
        """Remove the file (used when the job never started)."""
        self.close()
        if self.path is not None:
            try:
                self.path.unlink()
            except OSError:
                pass


def apply_slice_cap(cfg: PolicyConfig, cap_cores: float | None) -> None:
    """Honour a CPUQuota the admin set on my whole user slice: the scope can never get more."""
    if cap_cores is None:
        return
    c = max(int(cap_cores), 1)
    if c < cfg.max_cores:
        say(f"your user slice is capped at {cap_cores:g} cores by the admin; --max lowered to {c}")
        cfg.max_cores = c
    if c < cfg.min_cores:
        cfg.min_cores = c


def _wait(proc: subprocess.Popen, seconds: float) -> bool:
    """True if the process exited within `seconds`."""
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def run(cmd: list[str], cfg: PolicyConfig, interval: float, label: str | None = None,
        log: Path | bool = True, verbose: bool = False, presample: float = 2.0, pin: bool = True) -> int:
    """Run `cmd` under a steered CPUQuota; return its exit status (128+signal if it was killed).

    `log`: True for the default CSV (~/.local/state/cpusched/<unit>.csv), a Path, or False for none.
    With `pin` (default) every task in the scope is also bound to `quota` CPUs chosen on distinct,
    idle physical cores: a quota alone lets N workers smear over the whole machine, where CFS
    throttling and frequency scaling cost up to 5x throughput (see bench/).
    The controller keeps running until the scope is empty, not just until the main process exits,
    and keeps the job under the agreed cap even if its own log or stderr fail.
    """
    reason = systemd.available()
    if reason:
        raise SystemExit(f"cpusched: {reason}")
    if not systemd.cpu_delegated():
        raise SystemExit("cpusched: the cpu controller is not delegated to your user slice; "
                         "CPUQuota cannot be set without root on this machine")
    sampler = LoadSampler()
    me = sampler.uid
    slice_max = cg.read_cpu_max(cg.user_slice(me))
    apply_slice_cap(cfg, None if slice_max is None else slice_max[0])

    unit = systemd.unit_name(label or os.path.basename(cmd[0]))
    log_path = default_log_dir() / f"{unit}.csv" if log is True else (log or None)
    csvlog = CsvLog(log_path)                           # before launching: a bad path fails here

    meter = af.PerCpuMeter() if pin else None
    topo = af.topology(ncpu=sampler.ncpu) if pin else {}
    first = sampler.snapshot()
    time.sleep(presample)
    load = sampler.diff(first, sampler.snapshot())
    busy = meter.update() if meter else {}
    peers = 0
    try:
        peers = len(systemd.list_scopes())
    except systemd.SystemdError as e:
        say(f"warning: {e}")
    cfg.share = peers + 1
    tgt = target(load.nproc, load.others, cfg)
    # With other instances of mine running, join at the floor: they only notice the new share at
    # their next interval, so starting at the full share would oversubscribe the budget until then.
    state = PolicyState(quota=cfg.min_cores if peers else tgt)
    cpus: list[int] = af.choose(state.quota, [], busy, topo) if pin else []

    proc = systemd.launch_scope(unit, cmd, state.quota)
    # The pid survives systemd-run's exec and children inherit the mask at fork, so pinning the
    # main process now covers the workers it is about to start; later passes catch everything else.
    pinned = af.pin([proc.pid], cpus) if pin else None

    nsig = 0

    def forward(signum: int, _frame) -> None:
        nonlocal nsig
        nsig += 1
        name = signal.Signals(signum).name.removeprefix("SIG")
        send = name if nsig == 1 else ("TERM" if nsig == 2 else "KILL")
        try:
            systemd.kill_scope(unit, send)
            say(f"SIG{name}: sent SIG{send} to every process in {unit}.scope"
                + (" (one more sends SIGKILL)" if nsig == 2 else ""))
        except systemd.SystemdError as e:
            say(f"could not forward SIG{name}: {e}")

    # The job runs in its own session, so the terminal delivers Ctrl-C / hang-up to the wrapper only
    # and each signal reaches the job exactly once. A signal the wrapper inherited as ignored (nohup)
    # stays ignored.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) != signal.SIG_IGN:
            signal.signal(sig, forward)

    rc: int | None = None
    try:
        scope_path = None
        for _ in range(50):                               # the unit can take a moment to appear
            try:
                scope_path = systemd.control_group(unit)
            except systemd.SystemdError:
                scope_path = None
            if scope_path is not None or proc.poll() is not None:
                break
            time.sleep(0.1)
        if scope_path is None and proc.poll() is not None:
            rc = proc.returncode
            say(f"launch failed: systemd-run exited with {rc} before the scope appeared; nothing is running")
            csvlog.discard()
            return rc or 1
        sampler.scope_path = scope_path

        def tasks() -> list[int]:
            return af.scope_tasks(scope_path) if scope_path else []

        def repin() -> int | None:
            if not pin:
                return None
            return af.pin(tasks() or [proc.pid], cpus)

        def cap_now() -> float | None:
            cm = cg.read_cpu_max(scope_path) if scope_path else None
            return None if cm is None else cm[0]

        def emit(load: Load, tgt: int, action: str, pinned: int | None, cap: float | None) -> None:
            csvlog.write(log_row(load, cfg, tgt, state.quota, action, me, cpus, pinned, cap))
            if verbose:
                say(describe(load, cfg, tgt, state.quota, action, cpus, cap))

        say(f"unit {unit}.scope pid {proc.pid} quota {state.quota} cores "
            f"(min {cfg.min_cores}, max {cfg.max_cores}, margin {cfg.margin:g}, every {interval:g}s"
            + (f", cpus {af.fmt_cpus(cpus)}" if pin else ", no pinning") + ")"
            + (f"; log {log_path}" if log_path else ""))
        emit(load, tgt, "start", pinned, float(state.quota))

        prev = sampler.snapshot()
        leftover_warned = False
        while True:
            if rc is None:
                if _wait(proc, interval):
                    rc = proc.returncode
            else:
                time.sleep(interval)
            if rc is not None:
                left = tasks()
                if not left:
                    break
                if not leftover_warned:
                    say(f"main process exited with {rc} but {len(left)} tasks are still in {unit}.scope; "
                        f"keeping the cap on them (`cpusched stop {unit}` ends them)")
                    leftover_warned = True
            try:
                cur = sampler.snapshot()
                load = sampler.diff(prev, cur)
                prev = cur
                busy = meter.update() if meter else {}
                try:
                    cfg.share = max(len(systemd.list_scopes()), 1)
                except systemd.SystemdError as e:
                    say(f"warning: {e}")
                tgt = target(load.nproc, load.others, cfg)
                before = state.quota
                quota, action = decide(load.nproc, load.others, cfg, state,
                                       saturated=is_saturated(load.nproc, load.total_busy))
                if action in ("up", "down"):
                    try:
                        systemd.set_quota(unit, quota)
                    except systemd.SystemdError as e:
                        state.quota, state.up_count, state.up_min = before, 0, 0   # retry next interval
                        action += "-failed"
                        if proc.poll() is None or tasks():
                            say(f"set-property failed, will retry: {e}")
                    else:
                        if pin:
                            cpus = af.choose(quota, cpus, busy, topo)
                emit(load, tgt, action, repin(), cap_now())
            except Exception as e:                        # never leave the job uncontrolled
                say(f"controller error, still watching the job: {e!r}")
    except BaseException:
        say(f"wrapper is exiting on an error; sending SIGTERM to {unit}.scope so the job does not run on uncapped")
        try:
            systemd.kill_scope(unit, "TERM")
        except Exception:
            pass
        raise
    finally:
        csvlog.close()

    if rc is None:
        rc = proc.wait()
    if rc < 0:
        say(f"job killed by signal {-rc}")
        return 128 - rc
    return rc


def watch(cfg: PolicyConfig, interval: float, count: int | None = None) -> int:
    """Dry run: sample like `run` would and print the decision, without starting anything."""
    sampler = LoadSampler()
    slice_max = cg.read_cpu_max(cg.user_slice(sampler.uid))
    apply_slice_cap(cfg, None if slice_max is None else slice_max[0])
    meter = af.PerCpuMeter()
    topo = af.topology(ncpu=sampler.ncpu)

    def share() -> int:
        try:
            return max(len(systemd.list_scopes()), 1)
        except systemd.SystemdError:
            return 1

    prev = sampler.snapshot()
    time.sleep(min(interval, 2.0))
    load = sampler.diff(prev, sampler.snapshot())
    cfg.share = share()
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
        cfg.share = share()
        tgt = target(load.nproc, load.others, cfg)
        quota, action = decide(load.nproc, load.others, cfg, state,
                               saturated=is_saturated(load.nproc, load.total_busy))
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
    print(f"{'unit':50} {'cap':>5} {'cpus':16} {'tasks':>5} {'used_s':>8} {'thr_s':>7} {'nr_thr':>7}")
    for u in units:
        try:
            path = systemd.control_group(u)
        except systemd.SystemdError:
            path = None
        st = cg.read_cpu_stat(path) if path else {}
        cm = cg.read_cpu_max(path) if path else None
        cap = "max" if cm is None or cm[0] is None else f"{cm[0]:g}"
        tids = af.scope_tasks(path) if path else []
        cpus = ""
        for tid in tids:
            try:
                cpus = af.fmt_cpus(sorted(os.sched_getaffinity(tid)))
                break
            except OSError:
                continue
        print(f"{u:50} {cap:>5} {cpus:16} {len(tids):5d} {st.get('usage_usec', 0) / 1e6:8.0f} "
              f"{st.get('throttled_usec', 0) / 1e6:7.0f} {st.get('nr_throttled', 0):7d}")
    return 0
