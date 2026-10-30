from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re


MAX_PATCH_CHARS = 100_000
MAX_CONTEXT_CHARS = 20_000
PROTECTED_PARTS = {".env", ".git", ".ssh", ".aws", ".gnupg"}
SECRET_PATTERN = re.compile(
    r"(?i)((?:api[_-]?key|authorization|token|secret|password)\s*[:=]\s*)"
    r"([^\s,;]+)"
)


@dataclass(frozen=True)
class SecurityFinding:
    severity: str
    rule: str
    message: str


@dataclass(frozen=True)
class SecurityReport:
    findings: tuple[SecurityFinding, ...]

    @property
    def safe(self) -> bool:
        return not any(item.severity == "high" for item in self.findings)


def redact_untrusted_text(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    redacted = SECRET_PATTERN.sub(r"\1[REDACTED]", text)
    if len(redacted) > limit:
        return redacted[:limit] + "\n...[truncated by breakmycode]"
    return redacted


def review_patch(project: Path, diff: str) -> SecurityReport:
    findings: list[SecurityFinding] = []
    project = project.resolve()
    if not diff.strip():
        findings.append(SecurityFinding("high", "empty_patch", "Patch is empty."))
        return SecurityReport(tuple(findings))
    if len(diff) > MAX_PATCH_CHARS:
        findings.append(
            SecurityFinding("high", "patch_size", "Patch exceeds the size limit.")
        )
    if "GIT binary patch" in diff:
        findings.append(
            SecurityFinding("high", "binary_patch", "Binary patches are not allowed.")
        )

    paths: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw_path = line[4:].split("\t", 1)[0].strip()
        if raw_path == "/dev/null":
            continue
        if raw_path.startswith(("a/", "b/")):
            raw_path = raw_path[2:]
        paths.add(raw_path)
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts:
            findings.append(
                SecurityFinding(
                    "high", "path_escape", f"Patch path escapes the project: {raw_path}"
                )
            )
            continue
        if PROTECTED_PARTS.intersection(path.parts):
            findings.append(
                SecurityFinding(
                    "high", "protected_file", f"Patch targets protected path: {raw_path}"
                )
            )
        if path.name.startswith("test_") or "tests" in path.parts:
            findings.append(
                SecurityFinding(
                    "high",
                    "test_modification",
                    f"Code-bug patches may not modify tests: {raw_path}",
                )
            )
        candidate = project / Path(*path.parts)
        if candidate.exists():
            try:
                candidate.resolve().relative_to(project)
            except ValueError:
                findings.append(
                    SecurityFinding(
                        "high", "symlink_escape", f"Path resolves outside project: {raw_path}"
                    )
                )

    if not paths:
        findings.append(
            SecurityFinding("high", "invalid_diff", "No unified-diff file headers found.")
        )

    removed_assertions = sum(
        line.startswith("-") and not line.startswith("---") and "assert" in line
        for line in diff.splitlines()
    )
    if removed_assertions:
        findings.append(
            SecurityFinding(
                "high", "removed_assertion", "Patch removes one or more assertions."
            )
        )
    if "shell=True" in "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+")
    ):
        findings.append(
            SecurityFinding(
                "high", "shell_execution", "Patch introduces subprocess shell=True."
            )
        )
    return SecurityReport(tuple(findings))


def repair_unified_diff(project: Path, diff: str) -> str:
    """Fill in bare model-generated hunk headers from exact file context."""
    project = project.resolve()
    lines = diff.splitlines()
    current_path: Path | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ "):
            raw_path = line[4:].split("\t", 1)[0].strip()
            if raw_path != "/dev/null":
                if raw_path.startswith("b/"):
                    raw_path = raw_path[2:]
                candidate = PurePosixPath(raw_path)
                if not candidate.is_absolute() and ".." not in candidate.parts:
                    current_path = project / Path(*candidate.parts)
        if line.strip() == "@@" and current_path and current_path.is_file():
            end = index + 1
            while end < len(lines) and not lines[end].startswith(
                ("@@", "diff --git ", "--- ")
            ):
                end += 1
            body = lines[index + 1 : end]
            old_lines = [
                item[1:]
                for item in body
                if item.startswith((" ", "-")) and not item.startswith("--- ")
            ]
            new_lines = [
                item[1:]
                for item in body
                if item.startswith((" ", "+")) and not item.startswith("+++ ")
            ]
            source = current_path.read_text(encoding="utf-8").splitlines()
            start = _find_subsequence(source, old_lines)
            if start is not None:
                old_count = len(old_lines)
                new_count = len(new_lines)
                old_range = (
                    str(start + 1)
                    if old_count == 1
                    else f"{start + 1},{old_count}"
                )
                new_range = (
                    str(start + 1)
                    if new_count == 1
                    else f"{start + 1},{new_count}"
                )
                lines[index] = f"@@ -{old_range} +{new_range} @@"
        index += 1
    return "\n".join(lines) + ("\n" if diff.endswith("\n") else "")


def _find_subsequence(source: list[str], target: list[str]) -> int | None:
    if not target:
        return 0
    for index in range(len(source) - len(target) + 1):
        if source[index : index + len(target)] == target:
            return index
    return None

