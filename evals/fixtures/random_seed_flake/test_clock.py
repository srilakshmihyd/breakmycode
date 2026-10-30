from clock import unstable_clock


def test_unstable_clock():
    assert unstable_clock() == 0
