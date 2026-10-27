from cpusched.policy import PolicyConfig, PolicyState, decide, target


def cfg(**kw):
    base = dict(min_cores=2, max_cores=16, margin=4, up_steps=2)
    base.update(kw)
    return PolicyConfig(**base)


def test_target_is_machine_minus_others_minus_margin_clamped():
    c = cfg()
    assert target(80, 0, c) == 16          # capped at max
    assert target(80, 76, c) == 2          # floor
    assert target(80, 90, c) == 2          # others can exceed nproc on a saturated box; still the floor
    assert target(80, 60, c) == 16         # 80 - 60 - 4
    assert target(80, 61.5, c) == 14       # floor(14.5)


def test_share_splits_the_budget():
    assert target(80, 60, cfg(share=2)) == 8
    assert target(80, 60, cfg(share=0)) == 16     # share < 1 treated as 1


def test_down_is_immediate():
    c, s = cfg(), PolicyState(quota=16)
    assert decide(80, 70, c, s) == (6, "down")
    assert s.quota == 6 and s.up_count == 0


def test_up_needs_up_steps_intervals_and_takes_their_minimum():
    c, s = cfg(), PolicyState(quota=4)
    assert decide(80, 60, c, s) == (4, "hold-up")   # target 16
    assert decide(80, 66, c, s) == (10, "up")       # target 10 -> min(16, 10)
    assert s.quota == 10 and s.up_count == 0


def test_up_streak_resets_when_target_falls_back_to_quota():
    c, s = cfg(), PolicyState(quota=4)
    decide(80, 60, c, s)                            # hold-up
    assert decide(80, 72, c, s) == (4, "hold")      # target 4 == quota
    assert s.up_count == 0
    assert decide(80, 60, c, s) == (4, "hold-up")   # streak starts again


def test_up_steps_one_scales_up_at_once():
    c, s = cfg(up_steps=1), PolicyState(quota=2)
    assert decide(80, 0, c, s) == (16, "up")


def test_down_during_up_streak_clears_the_streak():
    c, s = cfg(), PolicyState(quota=8)
    decide(80, 60, c, s)                            # hold-up (target 16)
    assert decide(80, 74, c, s) == (2, "down")
    assert s.up_count == 0 and s.up_min == 0
