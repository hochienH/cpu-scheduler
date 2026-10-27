"""Command-line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import cgroup as cg
from .policy import PolicyConfig


def _policy_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--interval", type=float, default=30.0, help="seconds between decisions (default 30)")
    sp.add_argument("--min", dest="min_cores", type=int, default=2, help="floor in cores (default 2)")
    sp.add_argument("--max", dest="max_cores", type=int, default=None,
                    help="ceiling in cores (default: half of the machine's CPUs)")
    sp.add_argument("--margin", type=float, default=4.0,
                    help="cores to leave free on top of what others use (default 4)")
    sp.add_argument("--up-steps", type=int, default=2,
                    help="consecutive intervals that allow more before scaling up (default 2)")


def _policy(ns: argparse.Namespace) -> PolicyConfig:
    n = cg.nproc()
    max_cores = ns.max_cores if ns.max_cores is not None else max(n // 2, 1)
    if ns.min_cores < 1:
        raise SystemExit("cpusched: --min must be >= 1")
    if max_cores < ns.min_cores:
        raise SystemExit(f"cpusched: --max ({max_cores}) must be >= --min ({ns.min_cores})")
    if max_cores > n:
        raise SystemExit(f"cpusched: --max ({max_cores}) exceeds the machine's {n} CPUs")
    if ns.margin < 0:
        raise SystemExit("cpusched: --margin must be >= 0")
    if ns.interval < 1:
        raise SystemExit("cpusched: --interval must be >= 1 second")
    if ns.up_steps < 1:
        raise SystemExit("cpusched: --up-steps must be >= 1")
    return PolicyConfig(min_cores=ns.min_cores, max_cores=max_cores, margin=ns.margin, up_steps=ns.up_steps)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpusched",
                                description="Run a CPU-bound job under a CPU quota that follows what "
                                            "other users leave free (systemd user scope + cgroup v2).")
    p.add_argument("--version", action="version", version=f"cpusched {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a command under a steered CPUQuota",
                       usage="cpusched run [options] -- COMMAND [ARG ...]")
    _policy_args(r)
    r.add_argument("--label", help="name used in the scope unit and the log file (default: the command)")
    r.add_argument("--log", type=Path, help="CSV log path (default ~/.local/state/cpusched/<unit>.csv)")
    r.add_argument("--no-log", action="store_true", help="do not write the CSV log")
    r.add_argument("-v", "--verbose", action="store_true", help="print each decision to stderr")
    r.add_argument("--no-affinity", action="store_true",
                   help="only set the quota; do not pin the job's tasks to the allowed CPUs (slower, see docs)")
    r.add_argument("command", nargs=argparse.REMAINDER, help="the command to run (after --)")

    w = sub.add_parser("watch", help="dry run: print what `run` would decide, without starting a job")
    _policy_args(w)
    w.add_argument("--count", type=int, help="stop after this many decisions (default: forever)")

    rec = sub.add_parser("record", help="record machine-wide and per-user CPU usage to a CSV (for modelling)")
    rec.add_argument("--interval", type=float, default=1.0, help="seconds between samples (default 1)")
    rec.add_argument("--duration", type=float, help="stop after this many seconds (default: forever)")
    rec.add_argument("--out", type=Path, required=True, help="CSV file (appended to if it exists)")

    sub.add_parser("status", help="list my running cpusched scopes on this machine")

    s = sub.add_parser("stop", help="send a signal to every process of a cpusched scope")
    s.add_argument("unit", help="unit name as shown by `cpusched status` (with or without .scope)")
    s.add_argument("--signal", default="TERM", help="signal name (default TERM)")
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    if ns.cmd == "run":
        from .runner import default_log_dir, run
        from .systemd import unit_name
        cmd = list(ns.command)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            raise SystemExit("cpusched run: no command given (write it after --)")
        cfg = _policy(ns)
        log = None if ns.no_log else (ns.log or default_log_dir() / (unit_name(ns.label or cmd[0]) + ".csv"))
        return run(cmd, cfg, ns.interval, label=ns.label, log_path=log, verbose=ns.verbose,
                   pin=not ns.no_affinity)
    if ns.cmd == "watch":
        from .runner import watch
        return watch(_policy(ns), ns.interval, ns.count)
    if ns.cmd == "record":
        from .recorder import record
        return record(ns.out, ns.interval, ns.duration)
    if ns.cmd == "status":
        from .runner import status
        return status()
    if ns.cmd == "stop":
        from .systemd import SystemdError, kill_scope
        unit = ns.unit[:-len(".scope")] if ns.unit.endswith(".scope") else ns.unit
        try:
            kill_scope(unit, ns.signal)
        except SystemdError as e:
            raise SystemExit(f"cpusched: {e}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
