from counter import increment_with_two_workers


def test_two_workers_increment():
    assert increment_with_two_workers() == 2

