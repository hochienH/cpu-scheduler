import argparse
from pathlib import Path

import pytest

import cpusched.runner
from cpusched.cli import _policy, build_parser, main


def ns(**kw):
    base = dict(min_cores=2, max_cores=None, margin=4.0, up_steps=2, interval=30.0)
    base.update(kw)
    return argparse.Namespace(**base)


def test_policy_defaults_and_validation(monkeypatch):
    monkeypatch.setattr("cpusched.cli.cg.nproc", lambda: 80)
    cfg = _policy(ns())
    assert (cfg.min_cores, cfg.max_cores, cfg.margin, cfg.up_steps) == (2, 40, 4.0, 2)
    for bad in (ns(max_cores=1), ns(max_cores=81), ns(interval=0.5), ns(min_cores=0), ns(margin=-1), ns(up_steps=0)):
        with pytest.raises(SystemExit):
            _policy(bad)


def test_default_max_never_below_min_on_small_machines(monkeypatch):
    monkeypatch.setattr("cpusched.cli.cg.nproc", lambda: 3)
    cfg = _policy(ns())
    assert (cfg.min_cores, cfg.max_cores) == (2, 2)


def test_run_passes_command_and_log_choice(monkeypatch):
    calls = []
    monkeypatch.setattr(cpusched.runner, "run", lambda cmd, cfg, interval, **kw: calls.append((cmd, interval, kw)) or 0)
    monkeypatch.setattr("cpusched.cli.cg.nproc", lambda: 80)
    assert main(["run", "--interval", "5", "--no-log", "--", "python", "x.py", "--workers", "32"]) == 0
    cmd, interval, kw = calls[-1]
    assert cmd == ["python", "x.py", "--workers", "32"] and interval == 5 and kw["log"] is False
    assert main(["run", "--label", "lbl", "--", "python", "x.py"]) == 0
    assert calls[-1][2]["log"] is True and calls[-1][2]["label"] == "lbl"
    assert main(["run", "--log", "/tmp/x.csv", "--no-affinity", "python", "x.py"]) == 0   # `--` optional
    assert calls[-1][0] == ["python", "x.py"] and calls[-1][2]["log"] == Path("/tmp/x.csv")
    assert calls[-1][2]["pin"] is False
    with pytest.raises(SystemExit):
        main(["run", "--no-log"])                      # no command


def test_record_interval_is_validated(monkeypatch):
    monkeypatch.setattr("cpusched.recorder.record", lambda *a, **k: 0)
    with pytest.raises(SystemExit):
        main(["record", "--interval", "0", "--out", "/tmp/x.csv"])
    assert main(["record", "--interval", "1", "--out", "/tmp/x.csv"]) == 0


def test_stop_strips_scope_suffix(monkeypatch):
    seen = []
    monkeypatch.setattr("cpusched.systemd.kill_scope", lambda unit, sig: seen.append((unit, sig)))
    assert main(["stop", "cpusched-a-1.scope", "--signal", "KILL"]) == 0
    assert seen == [("cpusched-a-1", "KILL")]


def test_parser_has_all_subcommands():
    p = build_parser()
    for sub in ("run", "watch", "record", "status", "stop"):
        assert sub in p.format_help()
