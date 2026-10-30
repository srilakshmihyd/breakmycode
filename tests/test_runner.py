from breakmycode.models import TestRunResult as RunResult
from breakmycode.runner import calculate_test_stats, run_tests
from breakmycode.runner import _sanitized_test_environment


def test_calculates_flaky_failure_rate():
    results = [
        RunResult({"test_cart.py::test_first": "failed"}, {"test_cart.py::test_first": "boom"})
        for _ in range(6)
    ] + [
        RunResult({"test_cart.py::test_first": "passed"})
        for _ in range(14)
    ]

    stats = calculate_test_stats(results)["test_cart.py::test_first"]

    assert stats.is_flaky is True
    assert stats.failures == 6
    assert stats.passes == 14
    assert stats.failure_rate == 0.3
    assert stats.summary == "failed 6/20 (30%)"


def test_always_failing_test_is_not_flaky():
    results = [
        RunResult({"test_cart.py::test_empty": "failed"}) for _ in range(3)
    ]

    stats = calculate_test_stats(results)["test_cart.py::test_empty"]

    assert stats.is_flaky is False
    assert stats.summary == "failed 3/3 (100%)"


def test_pytest_environment_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret")
    monkeypatch.setenv("BREAKMYCODE_SAFE_VALUE", "visible")

    environment = _sanitized_test_environment()

    assert "NEBIUS_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert environment["BREAKMYCODE_SAFE_VALUE"] == "visible"


def test_repeated_runner_stops_when_pytest_collects_no_tests(tmp_path, monkeypatch):
    calls = []

    def no_tests(project, timeout_seconds):
        calls.append((project, timeout_seconds))
        return RunResult({}, pytest_output="no tests ran")

    monkeypatch.setattr("breakmycode.runner.run_pytest_once", no_tests)

    results = run_tests(tmp_path, 20)

    assert len(results) == 1
    assert len(calls) == 1

