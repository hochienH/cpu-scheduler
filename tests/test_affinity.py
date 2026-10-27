import os

from cpusched import affinity as af


def fake_sys(tmp_path, cores):
    """cores: list of (package, core) per cpu index."""
    root = tmp_path / "sys"
    for c, (pkg, core) in enumerate(cores):
        d = root / f"cpu{c}" / "topology"
        d.mkdir(parents=True)
        (d / "core_id").write_text(f"{core}\n")
        (d / "physical_package_id").write_text(f"{pkg}\n")
    return root


def test_topology_reads_siblings_and_falls_back(tmp_path):
    root = fake_sys(tmp_path, [(0, 0), (0, 1), (0, 0), (0, 1)])    # 2 cores x 2 threads: 0/2 and 1/3 siblings
    assert af.topology(root, 5) == {0: (0, 0), 1: (0, 1), 2: (0, 0), 3: (0, 1), 4: (-1, 4)}


def test_per_cpu_jiffies_and_meter(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu 10 0 0 10 0 0 0 0 0 0\ncpu0 5 0 0 5 0 0 0 0 0 0\ncpu1 5 0 0 5 0 0 0 0 0 0\nctxt 1\n")
    assert af.per_cpu_jiffies(proc) == {0: (5, 10), 1: (5, 10)}
    m = af.PerCpuMeter(proc)
    (proc / "stat").write_text("cpu 30 0 0 10 0 0 0 0 0 0\ncpu0 25 0 0 5 0 0 0 0 0 0\ncpu1 5 0 0 25 0 0 0 0 0 0\n")
    assert m.update() == {0: 1.0, 1: 0.0}


def test_choose_prefers_distinct_idle_cores_and_keeps_current():
    topo = {0: (0, 0), 1: (0, 1), 2: (0, 0), 3: (0, 1), 4: (0, 2), 5: (0, 2)}
    busy = {0: 0.0, 1: 0.9, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
    assert af.choose(2, [], busy, topo) == [0, 4]            # cpu 1 busy, 2 is 0's sibling -> core 2 via cpu 4
    assert af.choose(3, [0, 4], busy, topo) == [0, 3, 4]      # grow: keep 0,4; core 1's idle thread is cpu 3
    assert af.choose(1, [0, 3, 4], {0: 0.5, 3: 0.1, 4: 0.9}, topo) == [3]   # shrink: drop the busiest
    assert af.choose(4, [0, 4], busy, topo) == [0, 2, 3, 4]   # distinct cores exhausted: idlest sibling next
    assert af.choose(9, [], busy, topo) == [0, 1, 2, 3, 4, 5] # cannot exceed the machine


def test_scope_tasks_from_threads_or_procs(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "cgroup.threads").write_text("10\n11\n20\n")
    assert af.scope_tasks(scope) == [10, 11, 20]
    (scope / "cgroup.threads").unlink()
    (scope / "cgroup.procs").write_text("10\n")
    proc = tmp_path / "proc"
    (proc / "10" / "task" / "10").mkdir(parents=True)
    (proc / "10" / "task" / "11").mkdir()
    assert sorted(af.scope_tasks(scope, proc)) == [10, 11]
    assert af.scope_tasks(tmp_path / "gone") == []


def test_pin_self_and_skip_dead():
    allowed = sorted(os.sched_getaffinity(0))
    assert af.pin([os.getpid(), 2**22 - 1], allowed) == 1       # own pid ok, bogus tid skipped
    assert sorted(os.sched_getaffinity(0)) == allowed


def test_fmt_cpus():
    assert af.fmt_cpus([0, 1, 2, 5, 6, 9]) == "0-2,5-6,9"
    assert af.fmt_cpus([3]) == "3"
    assert af.fmt_cpus([]) == ""
