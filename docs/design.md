# Design

## Problem

The lab's GPU servers are shared by a dozen users. In September 2026 the admin capped every user at
2 CPUs (systemd `CPUQuota=200%` on `user-<uid>.slice`) after multiprocessing jobs with 32 workers hogged
whole machines. The cap was lifted on 2026-10-20 on the condition that we throttle ourselves: use the
idle cores when nobody is around, back off when others need the machine, never below 2 cores.

## Mechanism

- The job is started in a transient **systemd user scope** (`systemd-run --user --scope`). On these
  servers (Ubuntu 24.04, systemd 255, cgroup v2) the `cpu` controller is delegated to the user's
  own systemd instance (`user@<uid>.service/cgroup.subtree_control` contains `cpu`), so a user may
  set `CPUQuota=` on their own scopes without root.
- A control loop in the wrapper process reads the machine's CPU usage every `interval` seconds and
  rewrites the scope's quota with `systemctl --user set-property --runtime`. Verified live: the new
  value lands in the scope's `cpu.max` immediately and the kernel's CFS bandwidth controller
  enforces it from the next 100 ms period.
- The job is not modified. It may keep N worker processes; they are time-sliced over the K allowed
  cores. The admin can read the cap (`cpu.max`) and the throttling (`cpu.stat`) of the scope.

Processes cannot be moved into a scope after the fact: cgroup v2 only lets a writer migrate a process
if it can write `cgroup.procs` of the common ancestor, and that is the root-owned user slice. So the
job must be launched through `cpusched run`.

`nice` and `SCHED_IDLE` do not help here: CFS first splits time between cgroups by `cpu.weight`, and
every `user-<uid>.slice` has weight 100, so nice only reorders processes inside our own slice.

## Affinity: the quota alone is not enough

Measured with bench/throttle_overhead.py (same CPU-bound Pool.map, wall time, medians; CSVs in bench/).

hostC (36 CPUs, i9-10980XE), quota only, 3 reps:

| cap K | workers N | N/K | wall vs K uncapped workers |
|---|---|---|---|
| 2 / 4 / 8 | K | 1 | +0.3 % / -2.0 % / -0.8 % |
| 2 / 4 / 8 | 2K | 2 | +54 % / +54 % / +53 % |
| 2 / 4 / 8 | 4K | 4 | +173 % / +157 % / +307 % |
| 2 / 4 / 8 | 8K | 8 | +250 % / +444 % / +389 % |
| 2 / 4 / 8 | 16K | 16 | +452 % / +501 % / +389 % |

hostC, quota versus affinity versus both, 2 reps (wall vs K uncapped workers):

| cap K | workers N | quota only | affinity only | quota + affinity |
|---|---|---|---|---|
| 2 | 2 / 8 / 32 | +2 % / +170 % / +452 % | 0 % / -1 % / -1 % | -1 % / -1 % / +1 % |
| 4 | 4 / 16 / 64 | +1 % / +162 % / +502 % | 0 % / -2 % / -2 % | -1 % / -3 % / -1 % |
| 8 | 8 / 32 / 128 | -2 % / +324 % / +389 % | -2 % / -3 % / 0 % | -3 % / -4 % / 0 % |

hostA (80 CPUs), cap 2, 32 workers, 1 rep: quota only +248 %, affinity only +12 %, both -1 %.

With N workers and a K-core quota, CFS lets all N run for a burst each 100 ms period on N different
CPUs and then throttles them; every CPU looks a few percent busy, so intel_pstate (powersave) never
raises their frequency, and the SMT siblings get in each other's way. The job pays with up to 5x the
wall time while still "using 2 cores". Pinning all tasks of the scope to K CPUs on distinct physical
cores removes that: the K CPUs run flat out at turbo and the quota is never hit (throttled time in
cpu.stat stays near 0).

So `run` does both: CPUQuota as the cap the admin can read, `sched_setaffinity` on every task in the
scope (`cgroup.threads`) for throughput. The set is chosen with affinity.choose: keep the current
CPUs when the quota changes, add CPUs from physical cores not yet in the set, idlest core first
(core load = sum of its threads' busy fraction over the last interval), drop the busiest when
shrinking. The main process is pinned right after launch (its pid survives systemd-run's exec and
children inherit the mask), and every interval all tasks are re-pinned so processes started before a
change, or that reset their own mask, are caught. `--no-affinity` turns this off.

## Signal

- `total_busy` = busy cores over the interval from `/proc/stat` (user+nice+system+irq+softirq+steal
  jiffies over all jiffies, times the CPU count). This counts everything, kernel threads included.
- `mine` = usage delta of `user.slice/user-<me>.slice/cpu.stat` (all my processes, all sessions,
  all cpusched instances).
- `others = max(total_busy - mine, 0)`.
- Per-user usage of every other `user-<uid>.slice` and of `system.slice` is read for the logs (the
  files are world-readable); `procs_running` and `load1` are logged too.

Usage is not demand: on a saturated machine other users can only show the cores we leave them. The
margin handles that (see below).

## Policy

```
budget = (nproc - others - margin) / share        # share = my cpusched instances on this machine
target = clamp(floor(budget), min, max)
down: immediately when target < quota
up:   after `up_steps` consecutive intervals with target > quota, to the smallest of those targets
```

Why this converges: with quota K and others using everything else, `others = nproc - K`, so
`target = K - margin < K` and we scale down. With `free > margin` we scale up. The steady state keeps
at least `margin` cores free, which is also the signal that lets other users' demand show up as usage.

Defaults: interval 30 s, min 2, max nproc/2, margin 4, up_steps 2. These are placeholders until the
trace-driven simulation (sim/) picks them.

## Costs of the loop

- Re-evaluation itself: two file reads and one D-Bus call per interval, about 10 ms. Negligible.
- Conservatism: the margin and the up delay leave cores idle that we could have used.
- Flapping: noisy measurements make the quota oscillate; the asymmetric policy turns noise into a
  net loss of our capacity. Longer intervals smooth the measurement but react more slowly.
- Throttling overhead: N workers sharing K cores pay context switches and cache misses. To be
  measured with bench/ (same CPU-bound job: N workers under quota K versus K workers unthrottled).

The right interval depends on how fast other users' load changes, which is a property of this lab,
so it is measured (cpusched record) and simulated rather than guessed.

## Known limits

- One machine at a time; nothing coordinates across servers.
- If the wrapper dies (SIGKILL), the scope keeps running with the last quota. `cpusched status`
  shows it; `cpusched stop` signals every process in it.
- The wrapper ignores SIGINT because the terminal already delivers it to the job; a job that ignores
  SIGINT must be stopped with `cpusched stop` or SIGTERM to the wrapper (which is forwarded to the
  whole scope).
- `others` includes the kernel and system services; that is intended (they need the cores too).
- Pinning overrides whatever affinity the job sets for itself, once per interval.
- The CPU set is only recomputed when the quota changes; if another user lands on one of our CPUs
  meanwhile, the kernel's load balancer moves their (unpinned) task elsewhere, we do not move.
