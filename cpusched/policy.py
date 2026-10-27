"""The control law: how many cores to allow in the next interval.

Pure functions over plain numbers, so the trace-driven simulator (sim/) can replay exactly the code
that runs in production.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class PolicyConfig:
    min_cores: int = 2        # never below this (the floor agreed with the admin)
    max_cores: int = 8        # never above this (CLI default: nproc // 2)
    margin: float = 4.0       # cores to leave free on top of what others already use
    up_steps: int = 2         # consecutive intervals that allow more before we actually scale up
    share: int = 1            # cpusched instances of mine on this machine; the budget is split evenly


@dataclass
class PolicyState:
    quota: int                # cores currently allowed
    up_count: int = 0         # consecutive intervals in which the target was above quota
    up_min: int = 0           # smallest target seen during the current up-streak


def budget(nproc: int, others: float, cfg: PolicyConfig) -> float:
    """Cores I may use: the machine minus what others use minus the margin, split among my instances."""
    return (nproc - others - cfg.margin) / max(cfg.share, 1)


def target(nproc: int, others: float, cfg: PolicyConfig) -> int:
    t = math.floor(budget(nproc, others, cfg))
    return max(cfg.min_cores, min(cfg.max_cores, t))


def decide(nproc: int, others: float, cfg: PolicyConfig, state: PolicyState) -> tuple[int, str]:
    """Update `state` in place and return (quota, action).

    Scaling down is immediate. Scaling up needs `cfg.up_steps` consecutive intervals whose target is
    above the current quota, and then goes to the smallest of those targets, so one quiet interval
    in a noisy period does not hand the job a burst of cores it has to give back at once.
    """
    t = target(nproc, others, cfg)
    if t < state.quota:
        state.quota, state.up_count, state.up_min = t, 0, 0
        return t, "down"
    if t > state.quota:
        state.up_count += 1
        state.up_min = t if state.up_count == 1 else min(state.up_min, t)
        if state.up_count >= cfg.up_steps:
            state.quota, state.up_count, state.up_min = state.up_min, 0, 0
            return state.quota, "up"
        return state.quota, "hold-up"
    state.up_count, state.up_min = 0, 0
    return state.quota, "hold"
