from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Callable

from .context import collect_source_context
from .llm import LLMAnalyzer
from .runner import calculate_test_stats, run_tests
from .security import repair_unified_diff, review_patch


@dataclass(frozen=True)
class DetectionMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


def flaky_detection_metrics(
    expected_flaky: set[str], detected_flaky: set[str]
) -> DetectionMetrics:
    true_positives = len(expected_flaky & detected_flaky)
    false_positives = len(detected_flaky - expected_flaky)
    false_negatives = len(expected_flaky - detected_flaky)
    precision = (
        true_positives / (true_positives + false_positives)
        if true_positives + false_positives
        else 1.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if true_positives + false_negatives
        else 1.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return DetectionMetrics(
        true_positives,
        false_positives,
        false_negatives,
        precision,
        recall,
        f1,
    )


def classification_accuracy(expected: list[str], actual: list[str]) -> float:
    if len(expected) != len(actual):
        raise ValueError("Expected and actual classifications must have equal length")
    if not expected:
        return 1.0
    return sum(left == right for left, right in zip(expected, actual)) / len(expected)


@dataclass(frozen=True)
class EvaluationReport:
    metrics: dict[str, float | None]
    thresholds: dict[str, float]
    passed: bool
    cases: tuple[dict[str, Any], ...]
    model_usage: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "metrics": self.metrics,
            "thresholds": self.thresholds,
            "model_usage": self.model_usage,
            "cases": list(self.cases),
        }


DEFAULT_THRESHOLDS = {
    "min_flaky_precision": 0.8,
    "min_flaky_recall": 0.8,
    "min_classification_accuracy": 0.8,
    "min_patch_verification_rate": 0.8,
    "max_regression_rate": 0.0,
}


def run_evaluation(
    manifest_path: Path,
    analyzer: LLMAnalyzer,
    runs_override: int | None = None,
    verify_patches: bool = False,
    progress: Callable[[str], None] | None = None,
) -> EvaluationReport:
    manifest_path = manifest_path.resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        configuration = {"cases": raw, "thresholds": {}}
    else:
        configuration = raw
    if not configuration.get("cases"):
        raise ValueError("Evaluation manifest must contain at least one case")
    thresholds = {**DEFAULT_THRESHOLDS, **configuration.get("thresholds", {})}
    expected_flaky: set[str] = set()
    detected_flaky: set[str] = set()
    expected_classes: list[str] = []
    actual_classes: list[str] = []
    patch_attempts = 0
    verified_patches = 0
    regressions = 0
    model_usage: dict[str, int] = {}
    case_reports: list[dict[str, Any]] = []
    telemetry = getattr(analyzer, "telemetry", None)
    scan_id = getattr(analyzer, "scan_id", None)
    trace_enabled = (
        telemetry is not None
        and isinstance(scan_id, str)
        and bool(scan_id)
        and hasattr(telemetry, "emit")
    )
    if trace_enabled:
        telemetry.emit(
            scan_id,
            "evaluation",
            "evaluation_started",
            {"manifest": str(manifest_path), "cases": len(configuration.get("cases", []))},
        )

    for raw_case in configuration.get("cases", []):
        case_name = raw_case["name"]
        project = (manifest_path.parent / raw_case["project"]).resolve()
        runs = runs_override or int(raw_case.get("runs", 10))
        if progress:
            progress(f"Evaluating {case_name} with {runs} runs...")
        if trace_enabled:
            telemetry.emit(
                scan_id,
                "evaluation",
                "evaluation_case_started",
                {"case": case_name, "runs": runs},
            )
        results = run_tests(project, runs)
        stats = calculate_test_stats(results, runs)
        expected_case_flaky = set(raw_case.get("expected_flaky_tests", []))
        detected_case_flaky = {
            item.test_name for item in stats.values() if item.is_flaky
        }
        expected_flaky.update(f"{case_name}::{name}" for name in expected_case_flaky)
        detected_flaky.update(f"{case_name}::{name}" for name in detected_case_flaky)

        classifications: dict[str, str] = raw_case.get(
            "expected_classifications", {}
        )
        if not classifications and raw_case.get("expected_classification"):
            candidate_tests = list(raw_case.get("expected_failing_tests", []))
            if not candidate_tests:
                candidate_tests = list(expected_case_flaky)
            classifications = {
                test_name: raw_case["expected_classification"]
                for test_name in candidate_tests
            }
        actual_case_classes: dict[str, str] = {}
        patch_results: dict[str, dict[str, Any]] = {}
        for test_name, expected_class in classifications.items():
            expected_classes.append(expected_class)
            item = stats.get(test_name)
            if item is None or item.failures == 0:
                actual_classes.append("not_detected")
                actual_case_classes[test_name] = "not_detected"
                continue
            test_source, code_under_test = collect_source_context(project, test_name)
            analysis = analyzer.analyze(
                test_name,
                test_source,
                code_under_test,
                item.failure_outputs[0] if item.failure_outputs else "",
                item.is_flaky,
            )
            actual_classes.append(analysis.verdict)
            actual_case_classes[test_name] = analysis.verdict
            if analysis.model:
                model_usage[analysis.model] = model_usage.get(analysis.model, 0) + 1
            if verify_patches and analysis.diff and analysis.verdict == "code_bug":
                patch_attempts += 1
                verification = _verify_patch_in_copy(
                    project, analysis.diff, test_name, stats, runs
                )
                patch_results[test_name] = verification
                verified_patches += int(verification["verified"])
                regressions += int(bool(verification["regressions"]))
                if trace_enabled:
                    telemetry.emit(
                        scan_id,
                        "evaluation",
                        "evaluation_patch_verified",
                        {
                            "case": case_name,
                            "test": test_name,
                            "verified": verification["verified"],
                            "regressions": verification["regressions"],
                        },
                    )

        expected_failing = set(raw_case.get("expected_failing_tests", []))
        detected_failing = {
            item.test_name for item in stats.values() if item.failures > 0
        }
        case_reports.append(
            {
                "name": case_name,
                "project": str(project),
                "runs": runs,
                "expected_flaky": sorted(expected_case_flaky),
                "detected_flaky": sorted(detected_case_flaky),
                "expected_failing": sorted(expected_failing),
                "detected_failing": sorted(detected_failing),
                "expected_classifications": classifications,
                "actual_classifications": actual_case_classes,
                "patch_verification": patch_results,
            }
        )
        if trace_enabled:
            telemetry.emit(
                scan_id,
                "evaluation",
                "evaluation_case_completed",
                {
                    "case": case_name,
                    "detected_flaky": sorted(detected_case_flaky),
                    "detected_failing": sorted(detected_failing),
                },
            )

    detection = flaky_detection_metrics(expected_flaky, detected_flaky)
    class_accuracy = classification_accuracy(expected_classes, actual_classes)
    patch_rate = (
        verified_patches / patch_attempts
        if verify_patches and patch_attempts
        else (1.0 if verify_patches else None)
    )
    regression_rate = (
        regressions / patch_attempts
        if verify_patches and patch_attempts
        else (0.0 if verify_patches else None)
    )
    metrics = {
        "flaky_precision": detection.precision,
        "flaky_recall": detection.recall,
        "flaky_f1": detection.f1,
        "classification_accuracy": class_accuracy,
        "patch_verification_rate": patch_rate,
        "regression_rate": regression_rate,
        "patch_attempts": float(patch_attempts),
    }
    if trace_enabled:
        scan = telemetry.get_scan(scan_id)
        if scan:
            model_usage = {}
            for call in scan["model_calls"]:
                model = call["model"]
                model_usage[model] = model_usage.get(model, 0) + 1
    passed = (
        metrics["flaky_precision"] >= thresholds["min_flaky_precision"]
        and metrics["flaky_recall"] >= thresholds["min_flaky_recall"]
        and metrics["classification_accuracy"]
        >= thresholds["min_classification_accuracy"]
        and (
            not verify_patches
            or metrics["patch_verification_rate"]
            >= thresholds["min_patch_verification_rate"]
        )
        and (
            not verify_patches
            or metrics["regression_rate"] <= thresholds["max_regression_rate"]
        )
    )
    if trace_enabled:
        telemetry.emit(
            scan_id,
            "evaluation",
            "evaluation_completed",
            {"passed": passed, **metrics},
        )
    return EvaluationReport(
        metrics, thresholds, passed, tuple(case_reports), model_usage
    )


def _verify_patch_in_copy(
    project: Path,
    diff: str,
    test_name: str,
    before_stats: dict[str, Any],
    runs: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="breakmycode-eval-") as temp_dir:
        copy = Path(temp_dir) / "project"
        shutil.copytree(
            project,
            copy,
            ignore=shutil.ignore_patterns(
                ".git", ".env", ".venv", "venv", "__pycache__", ".pytest_cache"
            ),
        )
        diff = repair_unified_diff(copy, diff)
        security = review_patch(copy, diff)
        if not security.safe:
            return {
                "verified": False,
                "regressions": [],
                "error": "security_policy_rejected_patch",
            }
        subprocess.run(
            ["git", "init", "-q"],
            cwd=copy,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        checked = subprocess.run(
            ["git", "apply", "--check", "--unidiff-zero", "-"],
            cwd=copy,
            input=diff,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if checked.returncode != 0:
            return {
                "verified": False,
                "regressions": [],
                "error": "invalid_patch",
            }
        subprocess.run(
            ["git", "apply", "--unidiff-zero", "-"],
            cwd=copy,
            input=diff,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        after = calculate_test_stats(run_tests(copy, runs), runs)
        target = after.get(test_name)
        regression_names = [
            item.test_name
            for item in after.values()
            if item.failures > 0
            and (
                item.test_name not in before_stats
                or before_stats[item.test_name].failures == 0
            )
        ]
        return {
            "verified": bool(
                target is not None and target.failures == 0 and not regression_names
            ),
            "regressions": regression_names,
            "before": before_stats[test_name].summary,
            "after": target.summary if target else "test missing",
        }

