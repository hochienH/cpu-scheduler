"""Thin wrappers around `systemd-run --user` and `systemctl --user`. Only this module spawns processes."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .cgroup import CGROOT

_UNIT_BAD = re.compile(r"[^A-Za-z0-9_.:-]+")


class SystemdError(RuntimeError):
    pass


def available() -> str | None:
    """None if the user systemd instance is usable, otherwise a human-readable reason."""
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return "systemd-run / systemctl not found"
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return "XDG_RUNTIME_DIR is not set (no systemd user session: log in through ssh or a tty first)"
    r = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True)
    if r.returncode != 0:
        return f"systemctl --user does not answer: {r.stderr.strip()}"
    return None


def cpu_delegated(uid: int | None = None, root: Path = CGROOT) -> bool:
    """True if the user's systemd instance may set CPUQuota (the cpu controller is delegated to it)."""
    uid = os.getuid() if uid is None else uid
    p = root / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / "cgroup.subtree_control"
    try:
        return "cpu" in p.read_text().split()
    except OSError:
        return False


def unit_name(label: str, pid: int | None = None) -> str:
    """cpusched-<label>-<pid>, with the label reduced to characters systemd accepts in unit names."""
    pid = os.getpid() if pid is None else pid
    label = _UNIT_BAD.sub("-", label).strip("-.") or "job"
    return f"cpusched-{label[:60]}-{pid}"


def quota_arg(cores: int) -> str:
    return f"CPUQuota={int(cores) * 100}%"


def launch_scope(unit: str, cmd: list[str], cores: int) -> subprocess.Popen:
    """Start `cmd` inside a transient user scope with an initial CPUQuota.

    systemd-run --scope registers the scope and then execs the command in place, so the returned
    Popen's pid is the job's pid and its exit status is the job's exit status.
    """
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit,
            "-p", quota_arg(cores), "--", *cmd]
    return subprocess.Popen(argv)


def _ctl(*args: str) -> str:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemdError(f"systemctl --user {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def set_quota(unit: str, cores: int) -> None:
    _ctl("set-property", "--runtime", f"{unit}.scope", quota_arg(cores))


def control_group(unit: str, root: Path = CGROOT) -> Path | None:
    """Absolute cgroup directory of the scope, or None while systemd has not placed it yet."""
    out = _ctl("show", "-p", "ControlGroup", "--value", f"{unit}.scope").strip()
    return root / out.lstrip("/") if out else None


def parse_list_units(text: str) -> list[str]:
    """Unit names (without .scope) of the active scopes in `systemctl list-units --plain --no-legend` output."""
    names: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith(".scope") and parts[2] == "active":
            names.append(parts[0][: -len(".scope")])
    return names


def list_scopes() -> list[str]:
    """My active cpusched scopes on this machine (all sessions)."""
    return parse_list_units(_ctl("list-units", "--type=scope", "--plain", "--no-legend", "cpusched-*"))


def kill_scope(unit: str, sig: str = "TERM") -> None:
    """Send a signal to every process in the scope (the job and all its workers)."""
    _ctl("kill", f"--signal={sig}", f"{unit}.scope")


def stop_scope(unit: str) -> None:
    _ctl("stop", f"{unit}.scope")
