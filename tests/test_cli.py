import pytest

from cpusched.cli import build_parser, _policy


def test_run_command_after_double_dash():
    ns = build_parser().parse_args(["run", "--interval", "5", "--", "python", "x.py", "--workers", "32"])
    cmd = list(ns.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    assert cmd == ["python", "x.py", "--workers", "32"]
    assert ns.interval == 5


def test_policy_validation(monkeypatch):
    monkeypatch.setattr("cpusched.cli.cg.nproc", lambda: 80)
    ns = build_parser().parse_args(["run", "--", "true"])
    cfg = _policy(ns)
    assert (cfg.min_cores, cfg.max_cores, cfg.margin, cfg.up_steps) == (2, 40, 4.0, 2)
    with pytest.raises(SystemExit):
        _policy(build_parser().parse_args(["run", "--max", "1", "--", "true"]))      # max < min
    with pytest.raises(SystemExit):
        _policy(build_parser().parse_args(["run", "--max", "81", "--", "true"]))     # max > nproc
    with pytest.raises(SystemExit):
        _policy(build_parser().parse_args(["run", "--interval", "0.5", "--", "true"]))
