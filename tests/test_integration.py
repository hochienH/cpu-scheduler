"""Live tests: need a systemd user session with the cpu controller delegated (the lab servers have it)."""
import csv
import os
import signal
import subprocess
import sys
import time

import pytest

from cpusched import systemd
from cpusched.policy import PolicyConfig
from cpusched.runner import run

live = pytest.mark.skipif(systemd.available() is not None or not systemd.cpu_delegated(),
                          reason="no delegated systemd user session")

BUSY = "import time; t=time.time()\nwhile time.time()-t < {s}: pass"
POOL = ("import multiprocessing as mp, time\n"
        "def spin(_):\n    t=time.time()\n    while time.time()-t < {s}: pass\n"
        "if __name__ == '__main__':\n    mp.Pool({n}).map(spin, range({n}))")


@live
def test_workers_inherit_the_pin_and_quota_is_not_hit(tmp_path):
    log = tmp_path / "log.csv"
    cfg = PolicyConfig(min_cores=2, max_cores=2, margin=0, up_steps=1)
    rc = run([sys.executable, "-c", POOL.format(s=4, n=8)], cfg, interval=1, label="itest-pool",
             log_path=log, presample=0.5)
    assert rc == 0
    rows = list(csv.DictReader(open(log)))[1:]
    assert any(int(r["pinned"] or 0) >= 9 for r in rows)                     # 8 workers + the parent
    assert any(1.5 < float(r["scope_cores"] or 0) <= 2.3 for r in rows)      # 8 workers held to 2 cores
    assert all(float(r["scope_throttled"] or 0) < 0.2 for r in rows)         # by the pin, not the quota


@live
def test_run_steers_a_scope_and_returns_the_exit_code(tmp_path):
    log = tmp_path / "log.csv"
    cfg = PolicyConfig(min_cores=1, max_cores=2, margin=0, up_steps=1)
    rc = run([sys.executable, "-c", BUSY.format(s=3) + "\nraise SystemExit(7)"], cfg, interval=1,
             label="itest", log_path=log, presample=0.5)
    assert rc == 7
    rows = list(csv.DictReader(open(log)))
    assert rows[0]["action"] == "start"
    assert len(rows) >= 2
    assert {r["quota"] for r in rows} <= {"1", "2"}
    assert any(float(r["scope_cores"] or 0) > 0.5 for r in rows[1:])   # the busy loop was seen in the scope
    assert all(r["share"] == "1" for r in rows)
    assert all(len(r["cpus"].split(",")) >= 1 and r["cpus"] for r in rows)   # a CPU set was chosen
    assert any(int(r["pinned"] or 0) >= 1 for r in rows[1:])                 # and applied to live tasks
    assert all(r["scope_throttled"] == "" or float(r["scope_throttled"]) < 0.5 for r in rows[1:])  # pinned: quota rarely hit


@live
def test_sigterm_to_the_wrapper_kills_the_job():
    cmd = [sys.executable, "-m", "cpusched", "run", "--interval", "1", "--min", "1", "--max", "1",
           "--no-log", "--label", "itest-term", "--", sys.executable, "-c", BUSY.format(s=30)]
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
    p = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, text=True)
    time.sleep(3)
    p.send_signal(signal.SIGTERM)
    try:
        rc = p.wait(timeout=10)
    finally:
        if p.poll() is None:
            p.kill()
    assert rc == 128 + signal.SIGTERM
    assert "itest-term" not in " ".join(systemd.list_scopes())
