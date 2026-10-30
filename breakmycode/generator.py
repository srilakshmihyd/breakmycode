from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable
from uuid import uuid4

from .context import collect_generation_context
from .llm import LLMAnalyzer
from .models import PatchReview, TestRunResult
from .observability import EventStore
from .patches import PatchApplier, PatchError, approve_patch
from .runner import run_pytest_once
from .security import (
    SecurityFinding,
    SecurityReport,
    generated_test_paths,
    redact_untrusted_text,
    review_test_patch,
)


class TestGenerationWorkflow:
    """Generate, review, approve, apply, and execute new pytest tests."""

    def __init__(
        self,
        project: Path,
        analyzer: LLMAnalyzer,
        store: EventStore,
        input_fn: Callable[[str], str] = input,
        print_fn: Callable[[str], None] = print,
        ai_security_review: bool = True,
        timeout_seconds: int = 300,
    ):
        self.project = project.resolve()
        self.analyzer = analyzer
        self.store = store
        self.input_fn = input_fn
        self.print = print_fn
        self.ai_security_review = ai_security_review
        self.timeout_seconds = timeout_seconds
        self.patch_applier = PatchApplier(self.project)
        self.baseline: TestRunResult | None = None

    def run(self) -> dict:
        scan_id = "generate_" + uuid4().hex[:12]
        self.store.start_scan(scan_id, str(self.project))
        self.analyzer.bind_scan(scan_id, self.store)
        try:
            summary, status = self._run(scan_id)
        except Exception as exc:
            self.store.finish_scan(
                scan_id,
                "failed",
                {
                    "test_generation": True,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        self.store.finish_scan(scan_id, status, summary)
        self.print(f"\nSaved trace: breakmycode show {scan_id}")
        return {"scan_id": scan_id, "status": status, "summary": summary}

    def _run(self, scan_id: str) -> tuple[dict, str]:
        with self.store.span(scan_id, "validate"):
            self._validate_project()
        self.print(f"Test-generation ID: {scan_id}")
        self.print(
            "Plan: inspect source → baseline pytest → generate → security review "
            "→ approval → verify"
        )

        with self.store.span(scan_id, "context"):
            source_context, existing_tests = collect_generation_context(self.project)
            if source_context == "(no production Python files found)":
                raise ValueError("No production Python files were found in the project.")

        self.print("Running the existing pytest suite once as a baseline...")
        with self.store.span(scan_id, "baseline"):
            baseline = run_pytest_once(
                self.project, timeout_seconds=self.timeout_seconds
            )
            self.baseline = baseline
            self._print_test_totals("Baseline", baseline)
            self.store.emit(
                scan_id,
                "baseline",
                "pytest_run_completed",
                self._test_totals(baseline),
            )

        self.print("Asking Nemotron to propose additional pytest coverage...")
        with self.store.span(scan_id, "generate_tests"):
            feedback = ""
            deterministic_report = SecurityReport(())
            diff = ""
            for attempt in range(1, 5):
                diff = self.analyzer.generate_tests(
                    source_context, existing_tests, feedback=feedback
                )
                deterministic_report = self._deterministic_review(diff)
                if deterministic_report.safe or attempt == 4:
                    break
                self.print(
                    f"Proposal {attempt} failed deterministic/preflight validation; "
                    "requesting a corrected test..."
                )
                for finding in deterministic_report.findings:
                    self.print(f"  retry reason: {finding.message}")
                feedback = "; ".join(
                    finding.message for finding in deterministic_report.findings
                )
            self.store.emit(
                scan_id,
                "generate_tests",
                "test_patch_generated",
                {"paths": list(generated_test_paths(diff)), "characters": len(diff)},
            )

        with self.store.span(scan_id, "security_review"):
            report = self._security_review(diff, deterministic_report)
            self.store.emit(
                scan_id,
                "security_review",
                "test_patch_reviewed",
                {
                    "safe": report.safe,
                    "rules": [finding.rule for finding in report.findings],
                },
            )

        self.print("\nProposed generated tests:\n")
        self.print(diff.rstrip())
        if not report.safe:
            self.print("Test patch blocked by security policy:")
            for finding in report.findings:
                self.print(f"  [{finding.severity}] {finding.message}")
            return (
                {
                    "test_generation": True,
                    "approved": False,
                    "blocked": True,
                    "generated_paths": list(generated_test_paths(diff)),
                    "verified": False,
                },
                "security_blocked",
            )

        self.store.emit(scan_id, "approval", "approval_requested")
        approved = approve_patch(self.input_fn)
        self.store.emit(
            scan_id, "approval", "approval_received", {"approved": approved}
        )
        if not approved:
            self.print("Generated tests were not applied.")
            return (
                {
                    "test_generation": True,
                    "approved": False,
                    "blocked": False,
                    "generated_paths": list(generated_test_paths(diff)),
                    "verified": None,
                },
                "completed",
            )

        with self.store.span(scan_id, "apply"):
            try:
                branch = self.patch_applier.apply(diff, patch_kind="tests")
            except PatchError as exc:
                raise RuntimeError(f"Generated tests could not be applied: {exc}") from exc
        self.print(f"Applied generated tests on new branch: {branch}")

        self.print("Running pytest to verify the generated tests...")
        with self.store.span(scan_id, "verify"):
            verification = run_pytest_once(
                self.project, timeout_seconds=self.timeout_seconds
            )
            self._print_test_totals("Verification", verification)
            generated_paths = set(generated_test_paths(diff))
            generated_outcomes = {
                nodeid: outcome
                for nodeid, outcome in verification.outcomes.items()
                if nodeid.split("::", 1)[0] in generated_paths
            }
            baseline_failures = set(baseline.failures)
            new_failures = sorted(set(verification.failures) - baseline_failures)
            verified = (
                bool(generated_outcomes)
                and all(outcome == "passed" for outcome in generated_outcomes.values())
                and not new_failures
            )
            self.store.emit(
                scan_id,
                "verify",
                "generated_tests_verified",
                {
                    "verified": verified,
                    "generated_tests": generated_outcomes,
                    "new_failures": new_failures,
                },
            )

        if verified:
            self.print("Generated tests passed and introduced no new failures.")
        else:
            self.print("Generated-test verification failed; review the branch before use.")
            if new_failures:
                self.print("New failures: " + ", ".join(new_failures))
        return (
            {
                "test_generation": True,
                "approved": True,
                "blocked": False,
                "branch": branch,
                "generated_paths": sorted(generated_paths),
                "generated_tests": generated_outcomes,
                "new_failures": new_failures,
                "verified": verified,
            },
            "completed" if verified else "verification_failed",
        )

    def _security_review(
        self,
        diff: str,
        deterministic_report: SecurityReport | None = None,
    ) -> SecurityReport:
        report = deterministic_report or self._deterministic_review(diff)
        findings = list(report.findings)
        if report.safe and self.ai_security_review:
            try:
                model_review: PatchReview = self.analyzer.review_generated_tests(diff)
                if not model_review.safe:
                    findings.append(
                        SecurityFinding(
                            "high",
                            "adversarial_model_review",
                            model_review.explanation,
                        )
                    )
                findings.extend(
                    SecurityFinding("medium", "model_risk", risk)
                    for risk in model_review.risks
                )
            except Exception as exc:
                findings.append(
                    SecurityFinding(
                        "medium",
                        "review_unavailable",
                        f"AI security review unavailable: {type(exc).__name__}",
                    )
                )
        return SecurityReport(tuple(findings))

    def _deterministic_review(self, diff: str) -> SecurityReport:
        report = review_test_patch(self.project, diff)
        findings = list(report.findings)
        if report.safe:
            try:
                self.patch_applier.validate(diff, patch_kind="tests")
            except PatchError as exc:
                findings.append(
                    SecurityFinding(
                        "high",
                        "invalid_diff",
                        f"Git rejected the generated test diff: {exc}",
                    )
                )
        if not findings:
            preflight_finding = self._preflight_generated_tests(diff)
            if preflight_finding is not None:
                findings.append(preflight_finding)
        return SecurityReport(tuple(findings))

    def _preflight_generated_tests(self, diff: str) -> SecurityFinding | None:
        """Apply and execute generated tests only inside an isolated temporary copy."""
        if self.baseline is None:
            return SecurityFinding(
                "high", "preflight_unavailable", "Baseline pytest result is unavailable."
            )
        with tempfile.TemporaryDirectory(prefix="breakmycode-generated-tests-") as temp_dir:
            project_copy = Path(temp_dir) / "project"
            shutil.copytree(
                self.project,
                project_copy,
                symlinks=False,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".env",
                    ".venv",
                    "venv",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    "*.pyc",
                ),
            )
            applied = subprocess.run(
                ["git", "apply", "--unidiff-zero", "-"],
                cwd=project_copy,
                input=diff,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
            if applied.returncode != 0:
                return SecurityFinding(
                    "high",
                    "preflight_apply_failed",
                    "Generated test could not be applied in the isolated preflight copy.",
                )
            result = run_pytest_once(
                project_copy, timeout_seconds=self.timeout_seconds
            )
            paths = set(generated_test_paths(diff))
            generated = {
                nodeid: outcome
                for nodeid, outcome in result.outcomes.items()
                if nodeid.split("::", 1)[0] in paths
            }
            baseline_failures = set(self.baseline.failures)
            new_failures = sorted(set(result.failures) - baseline_failures)
            if not generated:
                return SecurityFinding(
                    "high",
                    "preflight_not_collected",
                    "Pytest did not collect the proposed generated test.",
                )
            if new_failures or any(outcome != "passed" for outcome in generated.values()):
                details = ", ".join(new_failures[:3]) or "generated test did not pass"
                if new_failures:
                    first_failure = result.failures.get(new_failures[0], "")
                    if first_failure:
                        details += "; " + redact_untrusted_text(first_failure, 700)
                return SecurityFinding(
                    "high",
                    "preflight_test_failed",
                    f"Generated test failed in the isolated preflight copy: {details}",
                )
        return None

    def _validate_project(self) -> None:
        if not self.project.is_dir():
            raise ValueError(f"Project folder does not exist: {self.project}")
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(
                "Test generation requires a Git repository so approved changes "
                "can be isolated on a new branch."
            )

    def _print_test_totals(self, label: str, result: TestRunResult) -> None:
        totals = self._test_totals(result)
        self.print(
            f"  {label}: {totals['passed']} passed, {totals['failed']} failed, "
            f"{totals['other']} skipped/other"
        )

    @staticmethod
    def _test_totals(result: TestRunResult) -> dict[str, int]:
        passed = sum(outcome == "passed" for outcome in result.outcomes.values())
        failed = sum(outcome == "failed" for outcome in result.outcomes.values())
        return {
            "passed": passed,
            "failed": failed,
            "other": len(result.outcomes) - passed - failed,
        }
