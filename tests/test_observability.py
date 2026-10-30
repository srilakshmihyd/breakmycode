from breakmycode.observability import EventStore
from breakmycode.pricing import estimate_cost_usd
import sqlite3


def test_scan_events_spans_and_model_calls_are_persisted(tmp_path):
    store = EventStore(tmp_path / "history.db")
    store.start_scan("scan_test", "/tmp/project")
    with store.span("scan_test", "test"):
        store.emit("scan_test", "test", "pytest_run_completed", {"failed": 1})
    store.record_model_call(
        "scan_test", "mock-model", "classification", 12.5, 10, 4, "success"
    )
    store.finish_scan("scan_test", "completed", {"verified": True})

    scan = store.get_scan("scan_test")

    assert scan is not None
    assert scan["status"] == "completed"
    assert scan["summary"] == {"verified": True}
    assert [event["event"] for event in scan["events"]] == [
        "started",
        "pytest_run_completed",
        "completed",
    ]
    assert scan["model_calls"][0]["model"] == "mock-model"


def test_aggregate_metrics_and_model_cost(tmp_path):
    store = EventStore(tmp_path / "history.db")
    store.start_scan("scan_metrics", "/tmp/project")
    store.emit(
        "scan_metrics",
        "approval",
        "approval_received",
        {"approved": True},
    )
    store.record_model_call(
        "scan_metrics",
        "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "classification",
        10,
        1_000_000,
        1_000_000,
        "success",
    )
    store.finish_scan("scan_metrics", "completed", {"verified": True})

    metrics = store.aggregate_metrics()

    assert metrics["scans"] == 1
    assert metrics["approved_patches"] == 1
    assert metrics["verified_fixes"] == 1
    assert metrics["models"][0]["estimated_cost_usd"] == 0.3
    assert estimate_cost_usd(
        "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", 1_000_000, 1_000_000
    ) == 0.3


def test_history_cleanup_removes_only_old_completed_scans(tmp_path):
    store = EventStore(tmp_path / "history.db")
    store.start_scan("old", "/tmp/old")
    store.finish_scan("old", "completed", {})
    store.start_scan("new", "/tmp/new")
    store.finish_scan("new", "completed", {})
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE scans SET completed_at = '2000-01-01T00:00:00+00:00' WHERE scan_id = 'old'"
        )

    deleted = store.prune(30)

    assert deleted == 1
    assert store.get_scan("old") is None
    assert store.get_scan("new") is not None

