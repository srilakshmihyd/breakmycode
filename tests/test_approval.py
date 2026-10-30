from unittest.mock import Mock

import pytest

from breakmycode.agent import DebugAgent
from breakmycode.models import Analysis, PatchReview, TestRunResult as RunResult
from breakmycode.observability import EventStore
from breakmycode.patches import approve_patch


@pytest.mark.parametrize("answer", ["", "n", "N", "yes", "anything else"])
def test_patch_is_rejected_without_exact_y(answer):
    input_fn = Mock(return_value=answer)

    assert approve_patch(input_fn) is False
    input_fn.assert_called_once_with("Apply this patch? [y/N] ")


def test_patch_is_approved_on_y():
    input_fn = Mock(return_value=" Y ")

    assert approve_patch(input_fn) is True


def test_cli_does_not_apply_declined_llm_patch(tmp_path, monkeypatch):
    analyzer = Mock()
    analyzer.bind_scan = Mock()
    analyzer.analyze.return_value = Analysis(
        verdict="code_bug",
        explanation="The function loses list order.",
        diff="--- a/cart.py\n+++ b/cart.py\n",
    )
    analyzer.review_patch.return_value = PatchReview(True, "Safe.")
    applier = Mock()
    monkeypatch.setattr(
        "breakmycode.agent.run_tests",
        lambda project, runs, on_progress, timeout_seconds: [
            RunResult(
                {"test_cart.py::test_first": "failed"},
                {"test_cart.py::test_first": "boom"},
            ),
            RunResult({"test_cart.py::test_first": "passed"}),
        ],
    )
    agent = DebugAgent(
        tmp_path,
        2,
        analyzer,
        EventStore(tmp_path / "history.db"),
        input_fn=Mock(return_value="n"),
        print_fn=Mock(),
    )
    agent.patch_applier = applier

    state = agent.run()

    assert state["applied_tests"] == []
    applier.apply.assert_not_called()


def test_agent_verifies_an_approved_patch(tmp_path, monkeypatch):
    analyzer = Mock()
    analyzer.bind_scan = Mock()
    analyzer.analyze.return_value = Analysis(
        verdict="code_bug",
        explanation="Production code is wrong.",
        diff="--- a/cart.py\n+++ b/cart.py\n@@ -1 +1 @@\n-old\n+new\n",
        confidence=0.95,
        model="super",
    )
    analyzer.review_patch.return_value = PatchReview(True, "Safe.")
    before = [
        RunResult({"test_cart.py::test_first": "failed"}, {"test_cart.py::test_first": "boom"}),
        RunResult({"test_cart.py::test_first": "passed"}),
    ]
    after = [
        RunResult({"test_cart.py::test_first": "passed"}),
        RunResult({"test_cart.py::test_first": "passed"}),
    ]
    test_runs = iter([before, after])
    monkeypatch.setattr(
        "breakmycode.agent.run_tests",
        lambda project, runs, on_progress, timeout_seconds: next(test_runs),
    )
    agent = DebugAgent(
        tmp_path,
        2,
        analyzer,
        EventStore(tmp_path / "history.db"),
        input_fn=Mock(return_value="y"),
        print_fn=Mock(),
    )
    agent.patch_applier = Mock()
    agent.patch_applier.apply.return_value = "breakmycode/fixes-test"

    state = agent.run()

    assert state["verified"] is True
    assert state["branch"] == "breakmycode/fixes-test"
    agent.patch_applier.apply.assert_called_once()

