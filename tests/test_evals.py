import pytest
import json
from unittest.mock import Mock

from breakmycode.evals import (
    classification_accuracy,
    flaky_detection_metrics,
    run_evaluation,
)
from breakmycode.models import Analysis


def test_flaky_detection_metrics():
    metrics = flaky_detection_metrics({"a", "b"}, {"b", "c"})

    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5


def test_classification_accuracy():
    assert classification_accuracy(
        ["code_bug", "test_bug"], ["code_bug", "uncertain"]
    ) == pytest.approx(0.5)


def test_end_to_end_eval_verifies_patch_in_isolated_copy(tmp_path):
    project = tmp_path / "fixture"
    project.mkdir()
    (project / "value.py").write_text("def value():\n    return 0\n", encoding="utf-8")
    (project / "test_value.py").write_text(
        "from value import value\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "value-bug",
                        "project": "fixture",
                        "runs": 1,
                        "expected_failing_tests": ["test_value.py::test_value"],
                        "expected_classifications": {
                            "test_value.py::test_value": "code_bug"
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    analyzer = Mock()
    analyzer.analyze.return_value = Analysis(
        verdict="code_bug",
        explanation="The implementation returns the wrong value.",
        diff="--- a/value.py\n+++ b/value.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 1\n",
        confidence=1.0,
        model="mock-super",
    )

    report = run_evaluation(manifest, analyzer, verify_patches=True)

    assert report.passed is True
    assert report.metrics["classification_accuracy"] == 1.0
    assert report.metrics["patch_verification_rate"] == 1.0
    assert report.model_usage == {"mock-super": 1}
    assert (project / "value.py").read_text(encoding="utf-8").endswith("return 0\n")

