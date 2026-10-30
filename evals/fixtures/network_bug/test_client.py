from client import health_status


def test_healthy_status():
    assert health_status() == 200
