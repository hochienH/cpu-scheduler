from cpusched import systemd


def test_unit_name_is_sanitised_and_bounded():
    assert systemd.unit_name("python build_corpus.py/x y", pid=42) == "cpusched-python-build_corpus.py-x-y-42"
    assert systemd.unit_name("///", pid=7) == "cpusched-job-7"
    assert len(systemd.unit_name("a" * 200, pid=1)) <= len("cpusched-") + 60 + len("-1")


def test_quota_arg():
    assert systemd.quota_arg(3) == "CPUQuota=300%"


def test_parse_list_units_keeps_only_active_scopes():
    text = ("cpusched-a-1.scope loaded active running /usr/bin/python x.py\n"
            "cpusched-b-2.scope loaded inactive dead something\n"
            "other.scope loaded active running x\n"
            "\n")
    assert systemd.parse_list_units(text) == ["cpusched-a-1", "other"]
