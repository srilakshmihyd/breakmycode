from __future__ import annotations

import ast
from pathlib import Path


MAX_SOURCE_CHARS = 20_000


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")[:MAX_SOURCE_CHARS]
    except (OSError, UnicodeDecodeError):
        return ""


def test_file_for_nodeid(project: Path, nodeid: str) -> Path | None:
    relative_name = nodeid.split("::", 1)[0]
    candidate = (project / relative_name).resolve()
    try:
        candidate.relative_to(project.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def collect_source_context(project: Path, nodeid: str) -> tuple[str, str]:
    """Return test source and likely local modules imported by that test."""
    test_file = test_file_for_nodeid(project, nodeid)
    if test_file is None:
        return "(test source not found)", "(code under test not found)"

    test_source = _safe_read(test_file)
    code_sections: list[str] = []
    try:
        tree = ast.parse(test_source)
    except SyntaxError:
        tree = ast.Module(body=[], type_ignores=[])

    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_names.add(node.module)

    for module_name in sorted(module_names):
        relative = Path(*module_name.split("."))
        candidates = [project / f"{relative}.py", project / relative / "__init__.py"]
        for candidate in candidates:
            if candidate.is_file() and candidate.resolve() != test_file:
                source = _safe_read(candidate)
                if source:
                    code_sections.append(
                        f"### {candidate.relative_to(project)}\n{source}"
                    )
                break

    return test_source, "\n\n".join(code_sections) or "(code under test not found)"

