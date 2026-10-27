# cpusched

Run a CPU-bound job on a shared Linux machine under a CPU quota that follows what other users leave
free. No root, no changes to the job.

```
cpusched run [--interval 30] [--min 2] [--max N] [--margin 4] [--up-steps 2] [--no-affinity] [-v] -- COMMAND [ARG ...]
cpusched watch [--count K]          # dry run: print the decisions without starting anything
cpusched status                     # my running cpusched scopes, their cap and throttling
cpusched stop UNIT [--signal TERM]  # signal every process in a scope
cpusched record --out trace.csv     # 1 Hz machine-wide and per-user CPU usage, for modelling
```

How it works: the command is started in a transient systemd user scope; every `interval` seconds the
wrapper measures how many cores everybody else is using and sets the scope's `CPUQuota` to
`clamp(nproc - others - margin, min, max)`. Scaling down is immediate; scaling up waits `up-steps`
intervals. The kernel enforces the cap (cgroup v2 `cpu.max`), and every task in the scope is pinned
to that many CPUs on distinct, idle physical cores: with the quota alone, 32 workers capped at 2
cores ran 3.5x slower than 2 workers; pinned as well, they run at full speed.
Details and the reasoning behind the defaults: [docs/design.md](docs/design.md). The survey of
existing tools that led to this design: [docs/survey.md](docs/survey.md).

Requirements: Linux with cgroup v2, a systemd user session (`XDG_RUNTIME_DIR` set, as after an ssh
login), and the `cpu` controller delegated to it (`cpusched run` checks and says so if not).
Python 3.10+, no dependencies.

## Install

```
git clone git@github.com:hochienH/cpu-scheduler.git
cd cpu-scheduler && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest            # live tests run only where a delegated systemd user session exists
```

Use the installed script from anywhere, e.g. in tmux:

```
~/cpu-scheduler/.venv/bin/cpusched run -v -- ~/.venv/bin/python src/build_corpus.py --workers 32
```

The log (one row per decision, CSV) lands in `~/.local/state/cpusched/<unit>.csv`.

## 中文說明

在共用機器上跑 CPU 密集的工作時，每隔一段時間看別人用了多少核，把自己的 CPU 配額調成
「機器總核數 − 別人用的 − 預留 margin」，夾在 min 與 max 之間；縮立刻縮，放要連續兩個 interval 都有空才放。
機制是 systemd user scope 的 CPUQuota（cgroup v2 `cpu.max`），由 kernel 強制，不用 root，工作本身不用改；
同時把 scope 裡所有 thread 釘到同樣數量的 CPU 上（不同實體核心、最閒的），否則 worker 數遠大於核數時會慢好幾倍。
工作一律用 `cpusched run -- 指令` 啟動（已在跑的行程搬不進 scope）。
