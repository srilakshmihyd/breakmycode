from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from dotenv import load_dotenv

from .agent import DebugAgent
from .evals import run_evaluation
from .generator import TestGenerationWorkflow
from .llm import LLMAnalyzer
from .observability import EventStore
from .redteam import run_red_team
from .serverless import build_source_archive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="breakmycode",
        description="Agentically find, explain, and safely repair flaky pytest tests.",
        epilog=(
            "Other commands: generate-tests, history, show, metrics, export-trace, "
            "red-team, eval, package-job, cleanup"
        ),
    )
    parser.add_argument("project_folder", type=Path)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=300, help="seconds per pytest run")
    parser.add_argument(
        "--no-ai-security-review",
        action="store_true",
        help="use deterministic patch policy checks without the additional AI review",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    load_dotenv()
    store = EventStore()

    if arguments and arguments[0] == "history":
        return _history(store, arguments[1:])
    if arguments and arguments[0] == "show":
        return _show(store, arguments[1:])
    if arguments and arguments[0] == "red-team":
        return _red_team(arguments[1:])
    if arguments and arguments[0] == "package-job":
        return _package_job(arguments[1:])
    if arguments and arguments[0] == "eval":
        return _eval(store, arguments[1:])
    if arguments and arguments[0] == "metrics":
        return _metrics(store, arguments[1:])
    if arguments and arguments[0] == "export-trace":
        return _export_trace(store, arguments[1:])
    if arguments and arguments[0] == "cleanup":
        return _cleanup(store, arguments[1:])
    if arguments and arguments[0] == "generate-tests":
        return _generate_tests(store, arguments[1:])
    if arguments and arguments[0] == "scan":
        arguments = arguments[1:]

    args = build_parser().parse_args(arguments)
    project = args.project_folder.expanduser().resolve()
    if not project.is_dir():
        print(f"error: project folder does not exist: {project}", file=sys.stderr)
        return 2
    if args.runs < 1 or args.timeout < 1:
        print("error: --runs and --timeout must be at least 1", file=sys.stderr)
        return 2

    analyzer = None
    if os.environ.get("NEBIUS_API_KEY"):
        analyzer = LLMAnalyzer(telemetry=store)
    else:
        print(
            "warning: NEBIUS_API_KEY is not set; tests will run without AI analysis.",
            file=sys.stderr,
        )

    agent = DebugAgent(
        project,
        args.runs,
        analyzer,
        store,
        ai_security_review=not args.no_ai_security_review,
        timeout_seconds=args.timeout,
    )
    try:
        state = agent.run()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if state.get("status") == "verification_failed":
        return 1
    if analyzer is None and state.get("summary", {}).get("failing_tests"):
        return 1
    return 0


def _history(store: EventStore, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode history")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    scans = store.list_scans(max(1, args.limit))
    if not scans:
        print("No scans recorded.")
        return 0
    for scan in scans:
        print(
            f"{scan['scan_id']}  {scan['status']:<20} "
            f"{scan['started_at']}  {scan['project']}"
        )
    return 0


def _show(store: EventStore, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode show")
    parser.add_argument("scan_id")
    args = parser.parse_args(argv)
    scan = store.get_scan(args.scan_id)
    if scan is None:
        print(f"error: unknown scan ID: {args.scan_id}", file=sys.stderr)
        return 1
    print(json.dumps(_scan_payload(scan), indent=2))
    return 0


def _red_team(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode red-team")
    parser.add_argument("project_folder", type=Path)
    args = parser.parse_args(argv)
    results = run_red_team(args.project_folder.expanduser().resolve())
    for result in results:
        status = "BLOCKED" if result.blocked else "FAILED"
        print(f"{status:<7} {result.name}: {', '.join(result.rules)}")
    passed = all(result.blocked for result in results)
    print(f"Red-team suite: {len(results) if passed else 0}/{len(results)} attacks blocked")
    return 0 if passed else 1


def _package_job(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode package-job")
    parser.add_argument("project_folder", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--scan-id")
    args = parser.parse_args(argv)
    scan_id = args.scan_id or "job_" + uuid4().hex[:12]
    archive = build_source_archive(args.project_folder, args.output, scan_id)
    print(f"Serverless-ready source archive: {archive}")
    print(f"Correlation scan ID: {scan_id}")
    return 0


def _eval(store: EventStore, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode eval")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--runs", type=int)
    parser.add_argument("--verify-patches", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not os.environ.get("NEBIUS_API_KEY"):
        print("error: NEBIUS_API_KEY is required for model evaluations", file=sys.stderr)
        return 1
    eval_id = "eval_" + uuid4().hex[:12]
    store.start_scan(eval_id, str(args.manifest.expanduser().resolve()))
    analyzer = LLMAnalyzer(telemetry=store, scan_id=eval_id)
    try:
        with store.span(eval_id, "evaluation"):
            report = run_evaluation(
                args.manifest,
                analyzer,
                runs_override=args.runs,
                verify_patches=args.verify_patches,
                progress=print,
            )
    except Exception as exc:
        store.finish_scan(
            eval_id,
            "failed",
            {"evaluation": True, "error": str(exc), "error_type": type(exc).__name__},
        )
        print(f"error: evaluation failed: {exc}", file=sys.stderr)
        return 1
    store.finish_scan(
        eval_id,
        "completed" if report.passed else "quality_gate_failed",
        {"evaluation": True, **report.metrics},
    )
    rendered = json.dumps(report.to_dict(), indent=2)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Evaluation report: {output}")
    print(f"Evaluation trace: breakmycode show {eval_id}")
    print(rendered)
    return 0 if report.passed else 1


def _metrics(store: EventStore, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode metrics")
    parser.parse_args(argv)
    print(json.dumps(store.aggregate_metrics(), indent=2))
    return 0


def _export_trace(store: EventStore, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode export-trace")
    parser.add_argument("scan_id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    scan = store.get_scan(args.scan_id)
    if scan is None:
        print(f"error: unknown scan ID: {args.scan_id}", file=sys.stderr)
        return 1
    rendered = json.dumps(_scan_payload(scan), indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"Trace exported: {output}")
    else:
        print(rendered, end="")
    return 0


def _cleanup(store: EventStore, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="breakmycode cleanup")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if not args.yes:
        print("No history deleted. Re-run with --yes to confirm cleanup.")
        return 0
    try:
        deleted = store.prune(args.days)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Deleted {deleted} scans older than {args.days} days.")
    return 0


def _generate_tests(store: EventStore, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="breakmycode generate-tests",
        description=(
            "Generate additional pytest tests, review them, and apply only after approval."
        ),
    )
    parser.add_argument("project_folder", type=Path)
    parser.add_argument("--timeout", type=int, default=300, help="seconds per pytest run")
    parser.add_argument(
        "--no-ai-security-review",
        action="store_true",
        help="use deterministic test policy checks without the additional AI review",
    )
    args = parser.parse_args(argv)
    project = args.project_folder.expanduser().resolve()
    if not project.is_dir():
        print(f"error: project folder does not exist: {project}", file=sys.stderr)
        return 2
    if args.timeout < 1:
        print("error: --timeout must be at least 1", file=sys.stderr)
        return 2
    if not os.environ.get("NEBIUS_API_KEY"):
        print("error: NEBIUS_API_KEY is required for test generation", file=sys.stderr)
        return 1

    workflow = TestGenerationWorkflow(
        project,
        LLMAnalyzer(telemetry=store),
        store,
        ai_security_review=not args.no_ai_security_review,
        timeout_seconds=args.timeout,
    )
    try:
        result = workflow.run()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if result["status"] == "completed" else 1


def _scan_payload(scan: dict) -> dict:
    return {
        "scan_id": scan["scan_id"],
        "project": scan["project"],
        "status": scan["status"],
        "started_at": scan["started_at"],
        "completed_at": scan["completed_at"],
        "summary": scan["summary"],
        "events": [
            {
                "timestamp": event["timestamp"],
                "node": event["node"],
                "event": event["event"],
                "duration_ms": event["duration_ms"],
                "data": json.loads(event["data_json"]),
            }
            for event in scan["events"]
        ],
        "model_calls": scan["model_calls"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
