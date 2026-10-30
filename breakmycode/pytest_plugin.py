"""Small pytest plugin used by runner subprocesses to serialize test outcomes."""

from __future__ import annotations

import json
import os
from pathlib import Path


_results: dict[str, dict[str, str]] = {}


def pytest_sessionstart(session):  # noqa: ARG001
    _results.clear()


def pytest_runtest_logreport(report):
    nodeid = report.nodeid
    if report.when == "call":
        if report.passed:
            _results[nodeid] = {"outcome": "passed", "failure": ""}
        elif report.failed:
            _results[nodeid] = {
                "outcome": "failed",
                "failure": report.longreprtext,
            }
        elif report.skipped:
            _results[nodeid] = {"outcome": "skipped", "failure": ""}
    elif report.failed:
        # Setup/teardown failures have no normal call report.
        _results[nodeid] = {
            "outcome": "failed",
            "failure": report.longreprtext,
        }


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    output_path = os.environ.get("BREAKMYCODE_RESULTS_FILE")
    if output_path:
        Path(output_path).write_text(json.dumps(_results), encoding="utf-8")

