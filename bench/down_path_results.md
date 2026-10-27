# Down and up paths under artificial load (hostA and hostB, 2026-10-28 00:36–00:48)

Driver: `bench/down_path.py run` (see its docstring). The job is `down_path.py spin` under
`cpusched run` with the defaults (interval 30, min 2, max 40 = nproc/2, margin 4, up_steps 2).
"Others" is an unmanaged `spin` started 75 s after the job and lasting 120 s, sized so that
others (baseline included) reaches the stated total. Both hosts have 80 CPUs. Raw logs in
`bench/downpath/` (`cpusched_<host>_<tag>.csv` is the per-interval cpusched log).

## gradual: job 30 workers, others raised to 44 (window where the machine is not saturated)

| host | baseline others | artificial | quota trace (one value per 30 s interval) |
|---|---|---|---|
| hostA | 2.9 | 41 | 40 40 40 → **31** (down) 31 31 → 32 (up) 32 → 40 (up) |
| hostB | 9.9 | 34 | 40 40 40 → **32** (down) 31 (down) 31 → 32 (up) 32 → 40 (up) |

Down fired at the first interval that saw others = 44 (target = 80 − 44 − 4 = 32). Up took two
steps: the interval right after the release still averaged part of the load, and a +1 jitter
interval before it (target 32 > quota 31, `hold-up`) had already started the up counter, so the
first up went to min(32, 40) = 32 and the second to 40. Cost: one interval at 32 instead of 40.
`scope_throttled` stayed 0 (30 workers on 31–40 CPUs).

## saturated: job 40 workers, others raised to 52 (demand 92 cores > 80)

| host | baseline others | artificial | quota trace |
|---|---|---|---|
| hostA | 2.3 | 50 | 40 40 40 → 28 → 25 → 24 (three downs) → 24 (hold-up) → 40 (up) |
| hostB | 7.9 | 44 | 40 40 40 → 27 → 27 (hold-up) → 25 (down) → 25 (hold-up) → 40 (up) |

The saturation shortcut (`total_busy >= 79.5`) never fired: the first interval after the load
started averaged the ramp (busy 78.1 / 76.1), and from then on the cap kept the machine below
saturation (busy 72–73 with others 50–52 and us 20–23). The margin invariant did what it is for:
each down let others' real demand show (others measured 47 → 50 → 52 as we backed off), and
the quota converged to nproc − others − margin = 24–25. Up went straight back to 40 two intervals
after the release. With 41 tasks on 40 CPUs `scope_throttled` was 0.01 s per 30 s (negligible).

Job throughput (chunks/s of the spin loop, whole 330 s run including the throttled window):
hostA 30 workers 30,403, 40 workers 35,708; hostB 30 workers 22,579, 40 workers 24,025.

## Reading

- Both paths work on both hosts with the defaults; no code change needed.
- Possible later tweak: do not let a +1 jitter start the up counter (require target ≥ quota + 2,
  or take the latest target instead of the minimum); it would save one interval per recovery.
- The saturation shortcut is rarely reached in practice because the controller's own back-off
  keeps `total_busy` under nproc − 0.5; it still matters when others alone fill the machine.
