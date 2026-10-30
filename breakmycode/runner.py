from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from collections.abc import Callable

from .models import TestRunResult, TestStats


ProgressCallback = Callable[[int, int, TestRunResult], None]

SENSITIVE_ENV_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PRIVATE_KEY",
)
SENSITIVE_ENV_PREFIXES = ("AWS_", "AZURE_", "GCP_", "NEBIUS_")


def _sanitized_test_environment() -> dict[str, str]:
    """Copy the process environment without exposing credentials to pytest."""
    environment = os.environ.copy()
    for name in list(environment):
        upper_name = name.upper()
        if upper_name.startswith(SENSITIVE_ENV_PREFIXES) or any(
            marker in upper_name for marker in SENSITIVE_ENV_MARKERS
        ):
            environment.pop(name, None)
    return environment


def run_pytest_once(project: Path, timeout_seconds: int = 300) -> TestRunResult:
    """Run pytest once in a fresh process and return per-test outcomes."""
    project = project.resolve()
    package_root = Path(__file__).resolve().parent.parent

    with tempfile.TemporaryDirectory(prefix="breakmycode-") as temp_dir:
        result_file = Path(temp_dir) / "results.json"
        env = _sanitized_test_environment()
        env["BREAKMYCODE_RESULTS_FILE"] = str(result_file)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONHASHSEED"] = "random"
        old_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(package_root), old_pythonpath) if part
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                ".",
                "--rootdir=.",
                "-q",
                "-p",
                "breakmycode.pytest_plugin",
                "-p",
                "no:cacheprovider",
            ],
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
        )

        # pytest uses exit code 2 for interrupted/collection failures, 3 for
        # internal errors, and 4 for command-line errors. The result plugin may
        # still write an empty JSON file for those cases, so inspect the exit
        # status before treating the run as a project with no tests. Exit code
        # 1 is a normal test failure and 5 means no tests were collected.
        if completed.returncode not in {0, 1, 5}:
            output = completed.stdout.strip()
            raise RuntimeError(
                "pytest could not collect or execute the project tests "
                f"(exit code {completed.returncode}). Run pytest with the same "
                "Python environment and install the project's dependencies."
                + (f"\n\n{output}" if output else "")
            )

        if not result_file.exists():
            raise RuntimeError(
                "pytest did not produce test results. Its output was:\n"
                + completed.stdout
            )

        raw_results = json.loads(result_file.read_text(encoding="utf-8"))

    outcomes = {
        test_name: data["outcome"] for test_name, data in raw_results.items()
    }
    failures = {
        test_name: data["failure"]
        for test_name, data in raw_results.items()
        if data["outcome"] == "failed"
    }
    return TestRunResult(outcomes, failures, completed.stdout)


def run_tests(
    project: Path,
    runs: int,
    on_progress: ProgressCallback | None = None,
    timeout_seconds: int = 300,
) -> list[TestRunResult]:
    results: list[TestRunResult] = []
    for run_number in range(1, runs + 1):
        result = run_pytest_once(project, timeout_seconds=timeout_seconds)
        results.append(result)
        if on_progress:
            on_progress(run_number, runs, result)
        # Repeating pytest cannot discover tests that were not present on the
        # first identical run. Stop early so callers can report the real issue.
        if not result.outcomes:
            break
    return results


def calculate_test_stats(
    results: list[TestRunResult], total_runs: int | None = None
) -> dict[str, TestStats]:
    """Aggregate pass/fail counts by pytest node ID."""
    denominator = len(results) if total_runs is None else total_runs
    test_names = sorted(
        {test_name for result in results for test_name in result.outcomes}
    )
    stats: dict[str, TestStats] = {}
    for test_name in test_names:
        passes = sum(r.outcomes.get(test_name) == "passed" for r in results)
        failures = sum(r.outcomes.get(test_name) == "failed" for r in results)
        failure_outputs = tuple(
            r.failures[test_name]
            for r in results
            if test_name in r.failures
        )
        stats[test_name] = TestStats(
            test_name=test_name,
            passes=passes,
            failures=failures,
            total_runs=denominator,
            failure_outputs=failure_outputs,
        )
    return stats

