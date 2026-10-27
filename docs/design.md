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
