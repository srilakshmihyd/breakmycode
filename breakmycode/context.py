from __future__ import annotations

import ast
from pathlib import Path


MAX_SOURCE_CHARS = 20_000
MAX_GENERATION_CHARS = 45_000
MAX_GENERATION_FILES = 30
IGNORED_SOURCE_PARTS = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    ".tox",
    ".nox",
}


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


def collect_generation_context(project: Path) -> tuple[str, str]:
    """Collect bounded production and test context for safe test generation."""
    project = project.resolve()
    production: list[Path] = []
    tests: list[Path] = []
    for candidate in sorted(project.rglob("*.py")):
        try:
            relative = candidate.relative_to(project)
            resolved = candidate.resolve()
            resolved.relative_to(project)
        except (OSError, ValueError):
            continue
        hidden_directory = any(part.startswith(".") for part in relative.parts[:-1])
        if (
            candidate.is_symlink()
            or hidden_directory
            or IGNORED_SOURCE_PARTS.intersection(relative.parts)
        ):
            continue
        if _is_test_path(relative):
            tests.append(candidate)
        else:
            production.append(candidate)

    source_context = _render_files(
        project,
        production[:MAX_GENERATION_FILES],
        MAX_GENERATION_CHARS,
    )
    test_context = _render_files(
        project,
        tests[:MAX_GENERATION_FILES],
        MAX_GENERATION_CHARS // 2,
    )
    return (
        source_context or "(no production Python files found)",
        test_context or "(no existing pytest files found)",
    )


def _is_test_path(path: Path) -> bool:
    return (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or "tests" in path.parts
        or "test" in path.parts[:-1]
    )


def _render_files(project: Path, paths: list[Path], limit: int) -> str:
    sections: list[str] = []
    used = 0
    for path in paths:
        source = _safe_read(path)
        if not source:
            continue
        header = f"### {path.relative_to(project)}\n"
        remaining = limit - used - len(header)
        if remaining <= 0:
            break
        section = header + source[:remaining]
        sections.append(section)
        used += len(section) + 2
        if used >= limit:
            break
    return "\n\n".join(sections)

