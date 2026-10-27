"""cpusched: run a CPU-bound job under a CPU quota that follows what other users leave free.

Mechanism: the job is started inside a transient systemd user scope (`systemd-run --user --scope`),
and a control loop in this process reads machine-wide CPU usage every interval and rewrites the
scope's CPUQuota (`systemctl --user set-property`). The kernel (cgroup v2 cpu.max) enforces the cap;
the job itself is never modified and does not need to be Python.
"""
__version__ = "0.1.0"
