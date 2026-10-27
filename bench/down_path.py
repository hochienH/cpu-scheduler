"""Exercise cpusched's down and up paths with artificial load.

    python bench/down_path.py spin --workers W --seconds T
        W processes burning CPU for T seconds; prints the total iteration rate at the end.
    python bench/down_path.py run --tag NAME --job-workers W --others-total O [...]
        1. measures the machine's baseline `others` (same sampler as cpusched),
        2. starts `spin --workers W` under `cpusched run`,
        3. after --others-after seconds starts an unmanaged `spin` with O - baseline workers
           (it counts as "others" because it is outside the managed scope) for --others-for seconds,
        4. waits for the job and prints the cpusched log as a table.
"""
from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _burn(deadline: float, q: mp.Queue) -> None:
    n = 0
    x = 1.0
    while time.monotonic() < deadline:
        for _ in range(20000):
            x = x * 1.0000001 + 1e-9
        n += 1
    q.put(n)


def spin(a: argparse.Namespace) -> int:
    deadline = time.monotonic() + a.seconds
    q: mp.Queue = mp.Queue()
    procs = [mp.Process(target=_burn, args=(deadline, q)) for _ in range(a.workers)]
    t0 = time.monotonic()
    for p in procs:
        p.start()
    total = sum(q.get() for _ in procs)
    for p in procs:
        p.join()
    el = time.monotonic() - t0
    print(f"spin workers={a.workers} seconds={a.seconds} chunks={total} per_s={total / el:.1f}", flush=True)
    return 0


def baseline_others(seconds: float = 3.0) -> tuple[float, int]:
    sys.path.insert(0, str(ROOT))
    from cpusched.sampler import LoadSampler  # noqa: E402

    s = LoadSampler()
    a = s.snapshot()
    time.sleep(seconds)
    b = s.snapshot()
    load = s.diff(a, b)
    return load.others, load.nproc


def run(a: argparse.Namespace) -> int:
    host = socket.gethostname().lower()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base, nproc = baseline_others()
    n_others = max(0, round(a.others_total - base))
    print(f"[{host}] nproc={nproc} baseline others={base:.1f} -> artificial others={n_others} "
          f"(target total {a.others_total}); job workers={a.job_workers} for {a.job_seconds}s; "
          f"others start +{a.others_after}s for {a.others_for}s", flush=True)
    log = out / f"cpusched_{host}_{a.tag}.csv"
    if log.exists():
        log.unlink()
    cmd = [str(ROOT / ".venv/bin/cpusched"), "run", "-v", "--interval", str(a.interval),
           "--label", f"downpath_{a.tag}", "--log", str(log)]
    if a.max:
        cmd += ["--max", str(a.max)]
    cmd += ["--", sys.executable, str(HERE / "down_path.py"), "spin",
            "--workers", str(a.job_workers), "--seconds", str(a.job_seconds)]
    job_log = open(out / f"job_{host}_{a.tag}.log", "w")
    t0 = time.monotonic()
    job = subprocess.Popen(cmd, stdout=job_log, stderr=subprocess.STDOUT)
    time.sleep(a.others_after)
    others = None
    if n_others:
        others = subprocess.Popen([sys.executable, str(HERE / "down_path.py"), "spin",
                                   "--workers", str(n_others), "--seconds", str(a.others_for)],
                                  stdout=open(out / f"others_{host}_{a.tag}.log", "w"),
                                  stderr=subprocess.STDOUT)
        print(f"[{host}] +{time.monotonic() - t0:.0f}s others started pid {others.pid}", flush=True)
        others.wait()
        print(f"[{host}] +{time.monotonic() - t0:.0f}s others finished", flush=True)
    rc = job.wait()
    job_log.close()
    print(f"[{host}] +{time.monotonic() - t0:.0f}s job exit {rc}", flush=True)
    print(open(out / f"job_{host}_{a.tag}.log").read().strip().splitlines()[-1], flush=True)
    with open(log) as f:
        rows = list(csv.DictReader(f))
    print(f"{'time':8} {'busy':>5} {'mngd':>5} {'oth':>5} {'tgt':>3} {'q':>3} {'cap':>3} "
          f"{'action':9} {'pin':>3} {'sc_cores':>8} {'thr_s':>6} {'nthr':>4}")
    for r in rows:
        print(f"{r['time'][11:]:8} {float(r['total_busy']):5.1f} {float(r['managed'] or 0):5.1f} "
              f"{float(r['others']):5.1f} {r['target']:>3} {r['quota']:>3} {r['cap']:>3} {r['action']:9} "
              f"{r['pinned']:>3} {r['scope_cores'] or '-':>8} {r['scope_throttled'] or '-':>6} "
              f"{r['scope_nr_throttled'] or '-':>4}")
    print(f"DONE {host} {a.tag} actions={sorted({r['action'] for r in rows})}", flush=True)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("spin")
    s.add_argument("--workers", type=int, required=True)
    s.add_argument("--seconds", type=float, required=True)
    r = sub.add_parser("run")
    r.add_argument("--tag", required=True)
    r.add_argument("--job-workers", type=int, required=True)
    r.add_argument("--job-seconds", type=float, default=330)
    r.add_argument("--others-total", type=float, required=True, help="others (cores) to reach, baseline included")
    r.add_argument("--others-after", type=float, default=75)
    r.add_argument("--others-for", type=float, default=120)
    r.add_argument("--interval", type=float, default=30)
    r.add_argument("--max", type=int, default=None)
    r.add_argument("--out-dir", default=str(HERE / "downpath"))
    a = ap.parse_args()
    return spin(a) if a.cmd == "spin" else run(a)


if __name__ == "__main__":
    raise SystemExit(main())
