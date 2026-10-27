import pytest

from cpusched import cgroup as cg
from cpusched.sampler import LoadSampler


def make_tree(tmp_path, slices, system_usec, busy, total, running=5, load="1.50", managed=None, uid=1):
    """managed: {scope name: usage_usec} of cpusched scopes under user-<uid>'s app.slice."""
    root, proc = tmp_path / "cg", tmp_path / "proc"
    for u, usec in slices.items():
        d = root / "user.slice" / f"user-{u}.slice"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec {usec}\nsystem_usec 0\n"
                                    "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n")
    app = root / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / "app.slice"
    for name, usec in (managed or {}).items():
        d = app / f"{name}.scope"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.stat").write_text(f"usage_usec {usec}\nnr_throttled 0\nthrottled_usec 0\n")
    (root / "user.slice" / "not-a-user.slice").mkdir(parents=True, exist_ok=True)
    (root / "system.slice").mkdir(exist_ok=True)
    (root / "system.slice" / "cpu.stat").write_text(f"usage_usec {system_usec}\n")
    proc.mkdir(exist_ok=True)
    idle = total - busy
    (proc / "stat").write_text(f"cpu {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
                               f"ctxt 1\nbtime 1\nprocesses 1\nprocs_running {running}\nprocs_blocked 0\n")
    (proc / "loadavg").write_text(f"{load} 0.50 0.20 1/100 123\n")
    return root, proc


def test_cgroup_helpers(tmp_path):
    root, proc = make_tree(tmp_path, {1: 10, 2: 20}, 5, busy=40, total=100)
    assert sorted(cg.list_user_slices(root)) == [1, 2]
    assert cg.read_cpu_stat(root / "user.slice" / "user-2.slice")["usage_usec"] == 20
    assert cg.read_cpu_stat(root / "nope") == {}
    assert cg.read_proc_stat(proc) == (40, 100)
    assert cg.read_procs_running(proc) == 5
    assert cg.read_load1(proc) == pytest.approx(1.5)
    d = root / "user.slice" / "user-1.slice"
    (d / "cpu.max").write_text("max 100000\n")
    assert cg.read_cpu_max(d) == (None, 100000)
    (d / "cpu.max").write_text("250000 100000\n")
    assert cg.read_cpu_max(d) == (2.5, 100000)
    assert cg.read_cpu_max(root / "nope") is None


def test_diff_splits_managed_and_others(tmp_path):
    root, proc = make_tree(tmp_path, {1: 0, 2: 0}, 0, busy=0, total=0, managed={"cpusched-a-1": 0, "other": 0})
    s = LoadSampler(root=root, procfs=proc, uid=1, ncpu=8)
    a = s.snapshot()
    # 10 s later, an 8-CPU box was 50% busy = 4 cores: uid 1 used 1 (0.6 of it in a cpusched scope),
    # uid 2 used 2, 1 core elsewhere (kernel). Only the cpusched scope is excluded from `others`.
    make_tree(tmp_path, {1: 10_000_000, 2: 20_000_000}, 5_000_000, busy=4000, total=8000, running=9,
              managed={"cpusched-a-1": 6_000_000, "other": 4_000_000})
    b = s.snapshot()
    b.t = a.t + 10
    load = s.diff(a, b)
    assert load.dt == pytest.approx(10)
    assert load.total_busy == pytest.approx(4.0)
    assert load.mine == pytest.approx(1.0)
    assert load.managed == pytest.approx(0.6)
    assert load.others == pytest.approx(3.4)
    assert load.per_uid[2] == pytest.approx(2.0)
    assert load.system == pytest.approx(0.5)
    assert load.procs_running == 9
    assert load.scope is None


def test_others_never_negative_and_new_slice_skipped(tmp_path):
    root, proc = make_tree(tmp_path, {1: 0}, 0, busy=0, total=0, managed={"cpusched-x-2": 0})
    s = LoadSampler(root=root, procfs=proc, uid=1, ncpu=8)
    a = s.snapshot()
    # jiffies and usec disagree slightly (rounding): managed > total_busy must not make others negative
    make_tree(tmp_path, {1: 10_100_000, 3: 1_000_000}, 0, busy=1000, total=8000,
              managed={"cpusched-x-2": 10_100_000, "cpusched-new-3": 5_000_000})
    b = s.snapshot()
    b.t = a.t + 10
    load = s.diff(a, b)
    assert load.others == 0.0
    assert load.managed == pytest.approx(1.01)   # the scope that appeared mid-interval is skipped once
    assert 3 not in load.per_uid                 # uid 3 appeared mid-interval: no baseline yet


def test_scope_counters(tmp_path):
    root, proc = make_tree(tmp_path, {1: 0}, 0, busy=0, total=0)
    scope = root / "scope"
    scope.mkdir()
    scope.joinpath("cpu.stat").write_text("usage_usec 0\nnr_throttled 0\nthrottled_usec 0\n")
    s = LoadSampler(root=root, procfs=proc, uid=1, ncpu=8, scope_path=scope)
    a = s.snapshot()
    make_tree(tmp_path, {1: 20_000_000}, 0, busy=2000, total=8000)
    scope.joinpath("cpu.stat").write_text("usage_usec 20000000\nnr_throttled 40\nthrottled_usec 3000000\n")
    b = s.snapshot()
    b.t = a.t + 10
    load = s.diff(a, b)
    assert load.scope == pytest.approx(2.0)
    assert load.scope_throttled == pytest.approx(0.3)
    assert load.scope_nr_throttled == 40
    scope.joinpath("cpu.stat").unlink()           # the scope went away (job exited)
    c = s.snapshot()
    assert c.scope_usec is None
