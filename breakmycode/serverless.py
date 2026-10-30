from __future__ import annotations

from pathlib import Path
import io
import json
import tarfile


EXCLUDED_PARTS = {
    ".env",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}
MAX_FILE_BYTES = 5_000_000


def build_source_archive(
    project: Path, destination: Path, scan_id: str | None = None
) -> Path:
    """Create a credential-safe source archive for a background job."""
    project = project.resolve()
    destination = destination.resolve()
    if not project.is_dir():
        raise ValueError(f"Project folder does not exist: {project}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(project.rglob("*")):
            relative = path.relative_to(project)
            if path.resolve() == destination:
                continue
            if EXCLUDED_PARTS.intersection(relative.parts):
                continue
            if path.is_symlink():
                continue
            if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
                archive.add(path, arcname=relative, recursive=False)
        if scan_id:
            payload = json.dumps({"scan_id": scan_id}).encode("utf-8")
            info = tarfile.TarInfo(".breakmycode-job.json")
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))
    return destination

