from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Any, Iterator

from .pricing import estimate_cost_usd


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_database_path() -> Path:
    configured = os.environ.get("BREAKMYCODE_DATA_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".breakmycode"
    return root / "history.db"


class EventStore:
    """SQLite-backed audit history for scans, events, traces, and model calls."""

    def __init__(self, database_path: Path | None = None):
        self.database_path = (database_path or default_database_path()).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    scan_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    summary_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node TEXT NOT NULL,
                    event TEXT NOT NULL,
                    duration_ms REAL,
                    data_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS events_scan_id_idx
                    ON events(scan_id, id);
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    estimated_cost_usd REAL,
                    status TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(model_calls)").fetchall()
            }
            if "estimated_cost_usd" not in columns:
                connection.execute(
                    "ALTER TABLE model_calls ADD COLUMN estimated_cost_usd REAL"
                )

    def start_scan(self, scan_id: str, project: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO scans(scan_id, project, status, started_at) VALUES (?, ?, ?, ?)",
                (scan_id, project, "running", _utc_now()),
            )

    def finish_scan(self, scan_id: str, status: str, summary: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE scans
                   SET status = ?, completed_at = ?, summary_json = ?
                   WHERE scan_id = ?""",
                (status, _utc_now(), json.dumps(summary, default=str), scan_id),
            )

    def emit(
        self,
        scan_id: str,
        node: str,
        event: str,
        data: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO events
                   (scan_id, timestamp, node, event, duration_ms, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    scan_id,
                    _utc_now(),
                    node,
                    event,
                    duration_ms,
                    json.dumps(data or {}, default=str),
                ),
            )

    @contextmanager
    def span(self, scan_id: str, node: str) -> Iterator[None]:
        self.emit(scan_id, node, "started")
        started = perf_counter()
        try:
            yield
        except Exception as exc:
            self.emit(
                scan_id,
                node,
                "failed",
                {"error_type": type(exc).__name__, "message": str(exc)[:500]},
                (perf_counter() - started) * 1000,
            )
            raise
        else:
            self.emit(
                scan_id,
                node,
                "completed",
                duration_ms=(perf_counter() - started) * 1000,
            )

    def record_model_call(
        self,
        scan_id: str,
        model: str,
        purpose: str,
        duration_ms: float,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        status: str,
    ) -> None:
        estimated_cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO model_calls
                   (scan_id, timestamp, model, purpose, duration_ms,
                    prompt_tokens, completion_tokens, estimated_cost_usd, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_id,
                    _utc_now(),
                    model,
                    purpose,
                    duration_ms,
                    prompt_tokens,
                    completion_tokens,
                    estimated_cost,
                    status,
                ),
            )

    def list_scans(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            scan = connection.execute(
                "SELECT * FROM scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()
            if scan is None:
                return None
            events = connection.execute(
                "SELECT * FROM events WHERE scan_id = ? ORDER BY id", (scan_id,)
            ).fetchall()
            model_calls = connection.execute(
                "SELECT * FROM model_calls WHERE scan_id = ? ORDER BY id", (scan_id,)
            ).fetchall()
        result = dict(scan)
        result["summary"] = json.loads(result.pop("summary_json"))
        result["events"] = [dict(row) for row in events]
        result["model_calls"] = [dict(row) for row in model_calls]
        return result

    def aggregate_metrics(self) -> dict[str, Any]:
        with self._connect() as connection:
            scans = connection.execute("SELECT status, summary_json FROM scans").fetchall()
            calls = connection.execute(
                """SELECT model, COUNT(*) AS calls,
                          AVG(duration_ms) AS average_latency_ms,
                          SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                          SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                          SUM(COALESCE(estimated_cost_usd, 0)) AS estimated_cost_usd,
                          SUM(CASE WHEN estimated_cost_usd IS NULL THEN 1 ELSE 0 END)
                              AS unpriced_calls,
                          SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
                   FROM model_calls GROUP BY model ORDER BY model"""
            ).fetchall()
            approval_rows = connection.execute(
                "SELECT data_json FROM events WHERE event = 'approval_received'"
            ).fetchall()
            review_rows = connection.execute(
                "SELECT data_json FROM events WHERE event = 'patch_reviewed'"
            ).fetchall()
        summaries = [json.loads(row["summary_json"]) for row in scans]
        verification_attempts = [
            item for item in summaries if item.get("verified") is not None
        ]
        evaluation_summaries = [item for item in summaries if item.get("evaluation")]
        patch_evaluation_summaries = [
            item
            for item in evaluation_summaries
            if isinstance(item.get("patch_verification_rate"), (int, float))
        ]
        approvals = [json.loads(row["data_json"]) for row in approval_rows]
        reviews = [json.loads(row["data_json"]) for row in review_rows]
        model_metrics = []
        for row in calls:
            item = dict(row)
            item["average_latency_ms"] = round(item["average_latency_ms"] or 0, 2)
            item["estimated_cost_usd"] = round(
                item["estimated_cost_usd"] or 0, 8
            )
            model_metrics.append(item)
        return {
            "scans": len(scans),
            "completed_scans": sum(row["status"] == "completed" for row in scans),
            "failed_scans": sum(row["status"] == "failed" for row in scans),
            "verification_attempts": len(verification_attempts),
            "verified_fixes": sum(item.get("verified") is True for item in summaries),
            "evaluation_runs": len(evaluation_summaries),
            "evaluation_patch_attempts": sum(
                item.get("patch_attempts", 0) for item in evaluation_summaries
            ),
            "average_evaluation_classification_accuracy": (
                sum(
                    item.get("classification_accuracy", 0)
                    for item in evaluation_summaries
                )
                / len(evaluation_summaries)
                if evaluation_summaries
                else None
            ),
            "average_evaluation_patch_verification_rate": (
                sum(
                    item.get("patch_verification_rate", 0)
                    for item in patch_evaluation_summaries
                )
                / len(patch_evaluation_summaries)
                if patch_evaluation_summaries
                else None
            ),
            "approval_requests": len(approvals),
            "approved_patches": sum(item.get("approved") is True for item in approvals),
            "security_reviews": len(reviews),
            "security_blocks": sum(item.get("safe") is False for item in reviews),
            "models": model_metrics,
        }

    def prune(self, older_than_days: int) -> int:
        if older_than_days < 1:
            raise ValueError("Retention must be at least one day")
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT scan_id FROM scans
                   WHERE completed_at IS NOT NULL AND completed_at < ?""",
                (cutoff_iso,),
            ).fetchall()
            scan_ids = [row["scan_id"] for row in rows]
            for scan_id in scan_ids:
                connection.execute("DELETE FROM events WHERE scan_id = ?", (scan_id,))
                connection.execute(
                    "DELETE FROM model_calls WHERE scan_id = ?", (scan_id,)
                )
                connection.execute("DELETE FROM scans WHERE scan_id = ?", (scan_id,))
        return len(scan_ids)

