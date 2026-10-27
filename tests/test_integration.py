"""Live tests: need a systemd user session with the cpu controller delegated (the lab servers have it)."""
import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cpusched.runner
from cpusched import systemd
from cpusched.policy import PolicyConfig
from cpusched.runner import run

live = pytest.mark.skipif(systemd.available() is not None or not systemd.cpu_delegated(),
                          reason="no delegated systemd user session")

BUSY = "import time; t=time.time()\nwhile time.time()-t < {s}: pass"
POOL = ("import multiprocessing as mp, time\n"
        "def spin(_):\n    t=time.time()\n    while time.time()-t < {s}: pass\n"
        "if __name__ == '__main__':\n    mp.Pool({n}).map(spin, range({n}))")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rows_of(log):
    return list(csv.DictReader(open(log)))


def no_scope_named(label):
    return not any(label in u for u in systemd.list_scopes())


def scripted(seq):
    """A stand-in for policy.decide that walks a fixed quota sequence, so set_quota really runs."""
    def decide(nproc, others, cfg, state, saturated=False):
        q = seq.pop(0) if seq else state.quota
        action = "down" if q < state.quota else ("up" if q > state.quota else "hold")
        state.quota = q
        return q, action
    return decide


@live
def test_run_sets_the_cap_and_returns_the_exit_code(tmp_path, monkeypatch):
    log = tmp_path / "log.csv"
    monkeypatch.setattr(cpusched.runner, "decide", scripted([1, 2, 1]))
    cfg = PolicyConfig(min_cores=1, max_cores=2, margin=0, up_steps=1)
    rc = run([sys.executable, "-c", BUSY.format(s=4) + "\nraise SystemExit(7)"], cfg, interval=1,
             label="itest", log=log, presample=0.5)
    assert rc == 7
    rows = rows_of(log)
    assert rows[0]["action"] == "start"
    acted = [(r["action"], r["quota"], r["cap"]) for r in rows[1:] if r["action"] in ("up", "down")]
    assert acted[:3] == [("down", "1", "1"), ("up", "2", "2"), ("down", "1", "1")]   # cpu.max followed
    assert all(r["share"] == "1" for r in rows)
    assert all(r["cpus"] for r in rows)                                             # a CPU set was chosen
    assert any(int(r["pinned"] or 0) >= 1 for r in rows[1:])                        # and applied
    assert any(float(r["scope_cores"] or 0) > 0.5 for r in rows[1:])
    assert no_scope_named("itest")


@live
def test_workers_inherit_the_pin_and_quota_is_not_hit(tmp_path):
    log = tmp_path / "log.csv"
    cfg = PolicyConfig(min_cores=2, max_cores=2, margin=0, up_steps=1)
    rc = run([sys.executable, "-c", POOL.format(s=4, n=8)], cfg, interval=1, label="itest-pool",
             log=log, presample=0.5)
    assert rc == 0
    rows = rows_of(log)[1:]
    assert any(int(r["pinned"] or 0) >= 9 for r in rows)                     # 8 workers + the parent
    assert any(1.5 < float(r["scope_cores"] or 0) <= 2.3 for r in rows)      # 8 workers held to 2 cores
    assert all(float(r["scope_throttled"] or 0) < 0.2 for r in rows)         # by the pin, not the quota


@live
def test_leftover_tasks_keep_the_cap_until_the_scope_is_empty():
    cmd = [sys.executable, "-c",
           "import subprocess, sys\n"
           "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], start_new_session=True)\n"
           "sys.exit(3)"]
    cfg = PolicyConfig(min_cores=1, max_cores=1, margin=0, up_steps=1)
    t = time.time()
    rc = run(cmd, cfg, interval=1, label="itest-left", log=False, presample=0.2)
    assert rc == 3
    assert time.time() - t >= 3.5                       # waited for the orphan, not just the main process
    assert no_scope_named("itest-left")


@live
def test_bad_log_path_fails_before_anything_starts():
    cfg = PolicyConfig(min_cores=1, max_cores=1, margin=0, up_steps=1)
    with pytest.raises(SystemExit):
        run(["sleep", "5"], cfg, interval=1, label="itest-badlog", log=Path("/proc/nonexistent/x.csv"))
    assert no_scope_named("itest-badlog")


@live
def test_missing_command_is_reported_as_launch_failure(tmp_path):
    cfg = PolicyConfig(min_cores=1, max_cores=1, margin=0, up_steps=1)
    log = tmp_path / "log.csv"
    rc = run(["/nonexistent/cmd"], cfg, interval=1, label="itest-nocmd", log=log, presample=0.2)
    assert rc != 0
    assert no_scope_named("itest-nocmd")


def _wrapper(label, extra=()):
    cmd = [sys.executable, "-m", "cpusched", "run", "--interval", "1", "--min", "1", "--max", "1",
           "--no-log", "--label", label, *extra, "--", sys.executable, "-c", BUSY.format(s=30)]
    env = dict(os.environ, PYTHONPATH=REPO)
    return subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize("sig,expected", [(signal.SIGTERM, 128 + signal.SIGTERM), (signal.SIGINT, 128 + signal.SIGINT)])
@live
def test_signal_to_the_wrapper_reaches_the_job(sig, expected):
    p = _wrapper(f"itest-sig{sig}")
    time.sleep(3)
    p.send_signal(sig)
    try:
        rc = p.wait(timeout=10)
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == expected
    assert no_scope_named(f"itest-sig{sig}")


@live
def test_repeated_signals_escalate_to_kill():
    ignore = ("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              "t=time.time()\nwhile time.time()-t < 30: pass")
    cmd = [sys.executable, "-m", "cpusched", "run", "--interval", "1", "--min", "1", "--max", "1",
           "--no-log", "--label", "itest-esc", "--", sys.executable, "-c", ignore]
    p = subprocess.Popen(cmd, env=dict(os.environ, PYTHONPATH=REPO), stderr=subprocess.PIPE, text=True)
    time.sleep(3)
    for _ in range(3):                                   # TERM (ignored), TERM again, KILL
        p.send_signal(signal.SIGTERM)
        time.sleep(0.5)
    try:
        rc = p.wait(timeout=10)
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == 128 + signal.SIGKILL
    assert no_scope_named("itest-esc")
