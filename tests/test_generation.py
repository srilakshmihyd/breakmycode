from unittest.mock import Mock

from breakmycode.generator import TestGenerationWorkflow as GenerationWorkflow
from breakmycode.models import PatchReview, TestRunResult as RunResult
from breakmycode.observability import EventStore


GENERATED_DIFF = """--- /dev/null
+++ b/tests/test_generated_cart.py
@@ -0,0 +1,2 @@
+def test_total():
+    assert 1 + 1 == 2
"""


def _project(tmp_path):
    (tmp_path / "cart.py").write_text("def total(items):\n    return sum(items)\n")
    (tmp_path / "test_cart.py").write_text("def test_existing():\n    assert True\n")
    return tmp_path


def _analyzer():
    analyzer = Mock()
    analyzer.generate_tests.return_value = GENERATED_DIFF
    analyzer.review_generated_tests.return_value = PatchReview(True, "Safe.")
    return analyzer


def test_declined_generated_tests_are_not_applied(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(
        "breakmycode.generator.TestGenerationWorkflow._validate_project",
        lambda self: None,
    )
    monkeypatch.setattr(
        "breakmycode.generator.run_pytest_once",
        lambda project, timeout_seconds: RunResult(
            {"test_cart.py::test_existing": "passed"}
        ),
    )
    monkeypatch.setattr(
        "breakmycode.generator.TestGenerationWorkflow._preflight_generated_tests",
        lambda self, diff: None,
    )
    workflow = GenerationWorkflow(
        project,
        _analyzer(),
        EventStore(tmp_path / "history.db"),
        input_fn=Mock(return_value="n"),
        print_fn=Mock(),
    )
    workflow.patch_applier = Mock()

    result = workflow.run()

    assert result["summary"]["approved"] is False
    workflow.patch_applier.apply.assert_not_called()


def test_approved_generated_tests_are_verified(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(
        "breakmycode.generator.TestGenerationWorkflow._validate_project",
        lambda self: None,
    )
    runs = iter(
        [
            RunResult({"test_cart.py::test_existing": "passed"}),
            RunResult(
                {
                    "test_cart.py::test_existing": "passed",
                    "tests/test_generated_cart.py::test_total": "passed",
                }
            ),
        ]
    )
    monkeypatch.setattr(
        "breakmycode.generator.run_pytest_once",
        lambda project, timeout_seconds: next(runs),
    )
    monkeypatch.setattr(
        "breakmycode.generator.TestGenerationWorkflow._preflight_generated_tests",
        lambda self, diff: None,
    )
    workflow = GenerationWorkflow(
        project,
        _analyzer(),
        EventStore(tmp_path / "history.db"),
        input_fn=Mock(return_value="y"),
        print_fn=Mock(),
    )
    workflow.patch_applier = Mock()
    workflow.patch_applier.apply.return_value = "breakmycode/fixes-generated"

    result = workflow.run()

    assert result["summary"]["verified"] is True
    assert result["summary"]["branch"] == "breakmycode/fixes-generated"
    workflow.patch_applier.apply.assert_called_once_with(
        GENERATED_DIFF, patch_kind="tests"
    )
