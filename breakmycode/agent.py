from __future__ import annotations

from pathlib import Path
import subprocess
from dataclasses import replace
from typing import Any, Callable, TypedDict
from uuid import uuid4
import warnings

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
from langgraph.graph import END, START, StateGraph

from .context import collect_source_context
from .llm import LLMAnalyzer
from .models import Analysis, PatchReview, TestStats
from .observability import EventStore
from .patches import PatchApplier, PatchError, approve_patch
from .runner import calculate_test_stats, run_tests
from .security import (
    SecurityFinding,
    SecurityReport,
    repair_unified_diff,
    review_patch,
)


class AgentState(TypedDict, total=False):
    scan_id: str
    project: str
    runs: int
    status: str
    results: list[Any]
    stats: dict[str, TestStats]
    analyses: list[dict[str, Any]]
    security_reports: list[SecurityReport]
    applied_tests: list[str]
    branch: str
    verification_stats: dict[str, TestStats]
    verified: bool
    summary: dict[str, Any]


class DebugAgent:
    """LangGraph coordinator for the observe-reason-act-verify lifecycle."""

    def __init__(
        self,
        project: Path,
        runs: int,
        analyzer: LLMAnalyzer | None,
        store: EventStore,
        input_fn: Callable[[str], str] = input,
        print_fn: Callable[[str], None] = print,
        ai_security_review: bool = True,
        timeout_seconds: int = 300,
    ):
        self.project = project.resolve()
        self.runs = runs
        self.analyzer = analyzer
        self.store = store
        self.input_fn = input_fn
        self.print = print_fn
        self.ai_security_review = ai_security_review
        self.timeout_seconds = timeout_seconds
        self.patch_applier = PatchApplier(self.project)
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("validate", self._validate)
        graph.add_node("test", self._test)
        graph.add_node("analyze", self._analyze)
        graph.add_node("security_review", self._security_review)
        graph.add_node("approval", self._approval)
        graph.add_node("verify", self._verify)
        graph.add_node("report", self._report)
        graph.add_edge(START, "validate")
        graph.add_edge("validate", "test")
        graph.add_conditional_edges(
            "test",
            lambda state: "analyze"
            if any(item.failures for item in state["stats"].values())
            else "report",
            {"analyze": "analyze", "report": "report"},
        )
        graph.add_edge("analyze", "security_review")
        graph.add_edge("security_review", "approval")
        graph.add_conditional_edges(
            "approval",
            lambda state: "verify" if state.get("applied_tests") else "report",
            {"verify": "verify", "report": "report"},
        )
        graph.add_edge("verify", "report")
        graph.add_edge("report", END)
        return graph.compile()

    def run(self) -> AgentState:
        scan_id = "scan_" + uuid4().hex[:12]
        self.store.start_scan(scan_id, str(self.project))
        if self.analyzer:
            self.analyzer.bind_scan(scan_id, self.store)
        initial: AgentState = {
            "scan_id": scan_id,
            "project": str(self.project),
            "runs": self.runs,
            "status": "running",
        }
        try:
            final = self.graph.invoke(
                initial,
                config={
                    "run_name": "breakmycode-scan",
                    "tags": ["breakmycode", "pytest-agent"],
                    "metadata": {"scan_id": scan_id, "project": str(self.project)},
                    "configurable": {"thread_id": scan_id},
                },
            )
        except Exception as exc:
            summary = {"error": str(exc), "error_type": type(exc).__name__}
            self.store.finish_scan(scan_id, "failed", summary)
            raise
        self.store.finish_scan(scan_id, final.get("status", "completed"), final["summary"])
        return final

    def _validate(self, state: AgentState) -> AgentState:
        scan_id = state["scan_id"]
        with self.store.span(scan_id, "validate"):
            if not self.project.is_dir():
                raise ValueError(f"Project folder does not exist: {self.project}")
            if self.runs < 1:
                raise ValueError("--runs must be at least 1")
            git_branch = self._current_branch()
            self.store.emit(
                scan_id,
                "validate",
                "project_validated",
                {"git_branch": git_branch, "runs": self.runs},
            )
            self.print(f"Scan ID: {scan_id}")
            self.print(
                "Plan: validate → repeat pytest → diagnose → security review "
                "→ approval → verify"
            )
        return {"status": "testing"}

    def _test(self, state: AgentState) -> AgentState:
        scan_id = state["scan_id"]
        self.print(f"Running pytest {self.runs} times in {self.project}...")
        with self.store.span(scan_id, "test"):
            results = run_tests(
                self.project,
                self.runs,
                on_progress=lambda current, total, result: self._progress(
                    scan_id, current, total, result
                ),
                timeout_seconds=self.timeout_seconds,
            )
            if not results or not any(result.outcomes for result in results):
                raise ValueError(
                    "pytest collected no tests. Add pytest tests or run "
                    "`breakmycode generate-tests <project_folder>` first."
                )
            stats = calculate_test_stats(results, self.runs)
            flaky = [item.test_name for item in stats.values() if item.is_flaky]
            self.store.emit(
                scan_id,
                "test",
                "test_aggregation_completed",
                {
                    "tests": len(stats),
                    "flaky_tests": flaky,
                    "failing_tests": [
                        item.test_name for item in stats.values() if item.failures
                    ],
                },
            )
        return {"results": results, "stats": stats, "status": "analyzing"}

    def _analyze(self, state: AgentState) -> AgentState:
        scan_id = state["scan_id"]
        analyses: list[dict[str, Any]] = []
        with self.store.span(scan_id, "analyze"):
            for item in state["stats"].values():
                if item.failures == 0:
                    continue
                if self.analyzer is None:
                    analysis = Analysis(
                        "uncertain",
                        "AI analysis is disabled because NEBIUS_API_KEY is not configured.",
                    )
                else:
                    test_source, code_under_test = collect_source_context(
                        self.project, item.test_name
                    )
                    failure = item.failure_outputs[0] if item.failure_outputs else ""
                    try:
                        analysis = self.analyzer.analyze(
                            item.test_name,
                            test_source,
                            code_under_test,
                            failure,
                            item.is_flaky,
                        )
                    except Exception as exc:
                        analysis = Analysis(
                            "uncertain",
                            f"AI request failed: {type(exc).__name__}: {exc}",
                        )
                analyses.append({"stats": item, "analysis": analysis})
                self.store.emit(
                    scan_id,
                    "analyze",
                    "failure_diagnosed",
                    {
                        "test": item.test_name,
                        "verdict": analysis.verdict,
                        "confidence": analysis.confidence,
                        "model": analysis.model,
                    },
                )
        return {"analyses": analyses, "status": "security_review"}

    def _security_review(self, state: AgentState) -> AgentState:
        scan_id = state["scan_id"]
        reports: list[SecurityReport] = []
        with self.store.span(scan_id, "security_review"):
            for finding in state["analyses"]:
                item: TestStats = finding["stats"]
                analysis: Analysis = finding["analysis"]
                if not analysis.diff:
                    reports.append(SecurityReport(()))
                    continue
                repaired_diff = repair_unified_diff(self.project, analysis.diff)
                if repaired_diff != analysis.diff:
                    analysis = replace(analysis, diff=repaired_diff)
                    finding["analysis"] = analysis
                report = review_patch(self.project, analysis.diff)
                additional = list(report.findings)
                if report.safe and self.ai_security_review and self.analyzer:
                    try:
                        model_review: PatchReview = self.analyzer.review_patch(
                            item.test_name, analysis.explanation, analysis.diff
                        )
                        if not model_review.safe:
                            additional.append(
                                SecurityFinding(
                                    "high",
                                    "adversarial_model_review",
                                    model_review.explanation,
                                )
                            )
                        for risk in model_review.risks:
                            additional.append(
                                SecurityFinding("medium", "model_risk", risk)
                            )
                    except Exception as exc:
                        additional.append(
                            SecurityFinding(
                                "medium",
                                "review_unavailable",
                                f"AI security review unavailable: {type(exc).__name__}",
                            )
                        )
                report = SecurityReport(tuple(additional))
                reports.append(report)
                self.store.emit(
                    scan_id,
                    "security_review",
                    "patch_reviewed",
                    {
                        "test": item.test_name,
                        "safe": report.safe,
                        "rules": [entry.rule for entry in report.findings],
                    },
                )
        return {"security_reports": reports, "status": "awaiting_approval"}

    def _approval(self, state: AgentState) -> AgentState:
        scan_id = state["scan_id"]
        applied: list[str] = []
        branch = ""
        with self.store.span(scan_id, "approval"):
            self._print_findings(state)
            for index, finding in enumerate(state["analyses"]):
                item: TestStats = finding["stats"]
                analysis: Analysis = finding["analysis"]
                if not analysis.diff:
                    continue
                report = state["security_reports"][index]
                self.print("\nProposed patch:\n")
                self.print(analysis.diff.rstrip())
                if not report.safe:
                    self.print("Patch blocked by security policy:")
                    for security_finding in report.findings:
                        self.print(
                            f"  [{security_finding.severity}] {security_finding.message}"
                        )
                    continue
                self.store.emit(
                    scan_id, "approval", "approval_requested", {"test": item.test_name}
                )
                approved = approve_patch(self.input_fn)
                self.store.emit(
                    scan_id,
                    "approval",
                    "approval_received",
                    {"test": item.test_name, "approved": approved},
                )
                if not approved:
                    self.print("Patch not applied.")
                    continue
                try:
                    branch = self.patch_applier.apply(analysis.diff)
                except PatchError as exc:
                    self.print(f"Patch was not applied: {exc}")
                    continue
                applied.append(item.test_name)
                self.print(f"Applied on new branch: {branch}")
        return {
            "applied_tests": applied,
            "branch": branch,
            "status": "verifying" if applied else "completed",
        }

    def _verify(self, state: AgentState) -> AgentState:
        scan_id = state["scan_id"]
        self.print(f"\nVerifying approved changes with {self.runs} fresh test runs...")
        with self.store.span(scan_id, "verify"):
            results = run_tests(
                self.project,
                self.runs,
                on_progress=lambda current, total, result: self._progress(
                    scan_id, current, total, result, node="verify"
                ),
                timeout_seconds=self.timeout_seconds,
            )
            stats = calculate_test_stats(results, self.runs)
            verified = all(
                stats.get(test_name) is not None
                and stats[test_name].failures == 0
                for test_name in state["applied_tests"]
            )
            regressions = [
                item.test_name
                for item in stats.values()
                if item.failures > 0
                and (
                    item.test_name not in state["stats"]
                    or state["stats"][item.test_name].failures == 0
                )
            ]
            if regressions:
                verified = False
            self.store.emit(
                scan_id,
                "verify",
                "verification_completed",
                {"verified": verified, "regressions": regressions},
            )
        return {
            "verification_stats": stats,
            "verified": verified,
            "status": "completed" if verified else "verification_failed",
        }

    def _report(self, state: AgentState) -> AgentState:
        with self.store.span(state["scan_id"], "report"):
            if "analyses" not in state:
                self._print_flaky(state["stats"])
            if state.get("verification_stats"):
                self.print("\nVerification results:")
                for test_name in state.get("applied_tests", []):
                    before = state["stats"].get(test_name)
                    after = state["verification_stats"].get(test_name)
                    if before and after:
                        self.print(
                            f"  {test_name}: before {before.summary}; after {after.summary}"
                        )
                self.print(
                    "  Fix verified." if state.get("verified") else "  Verification failed."
                )
            stats = state["stats"]
            summary = {
                "tests": len(stats),
                "flaky_tests": [item.test_name for item in stats.values() if item.is_flaky],
                "failing_tests": [
                    item.test_name for item in stats.values() if item.failures
                ],
                "applied_tests": state.get("applied_tests", []),
                "branch": state.get("branch", ""),
                "verified": state.get("verified"),
            }
            self.print(f"\nSaved trace: breakmycode show {state['scan_id']}")
        return {"summary": summary, "status": state.get("status", "completed")}

    def _print_findings(self, state: AgentState) -> None:
        self._print_flaky(state["stats"])
        self.print("\nAI analysis:")
        labels = {
            "code_bug": "code bug",
            "test_bug": "test expectation bug",
            "uncertain": "uncertain",
        }
        for finding in state["analyses"]:
            item: TestStats = finding["stats"]
            analysis: Analysis = finding["analysis"]
            confidence = f"{analysis.confidence:.0%}" if analysis.confidence else "unknown"
            model = f" via {analysis.model}" if analysis.model else ""
            self.print(f"\n{item.test_name} — {item.summary}")
            self.print(
                f"  Verdict: {labels[analysis.verdict]} "
                f"(confidence {confidence}{model})"
            )
            self.print(f"  {analysis.explanation}")

    def _print_flaky(self, stats: dict[str, TestStats]) -> None:
        flaky = [item for item in stats.values() if item.is_flaky]
        self.print("\nFlaky tests:")
        if not flaky:
            self.print("  None found.")
        for item in flaky:
            self.print(f"  {item.test_name}: {item.summary}")

    def _progress(
        self,
        scan_id: str,
        current: int,
        total: int,
        result: Any,
        node: str = "test",
    ) -> None:
        passed = sum(value == "passed" for value in result.outcomes.values())
        failed = sum(value == "failed" for value in result.outcomes.values())
        self.print(f"  run {current}/{total}: {passed} passed, {failed} failed")
        self.store.emit(
            scan_id,
            node,
            "pytest_run_completed",
            {"run": current, "total": total, "passed": passed, "failed": failed},
        )

    def _current_branch(self) -> str:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "not-a-git-repository"
