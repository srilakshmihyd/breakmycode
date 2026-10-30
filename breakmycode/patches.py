from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import subprocess

from .security import repair_unified_diff, review_patch


InputFunction = Callable[[str], str]


def approve_patch(input_fn: InputFunction = input) -> bool:
    """Only an exact, case-insensitive 'y' grants approval."""
    return input_fn("Apply this patch? [y/N] ").strip().lower() == "y"


class PatchError(RuntimeError):
    pass


class PatchApplier:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.branch_name: str | None = None

    def apply(self, diff: str) -> str:
        if not diff.strip():
            raise PatchError("The model returned an empty patch.")
        diff = repair_unified_diff(self.project, diff)
        report = review_patch(self.project, diff)
        if not report.safe:
            messages = "; ".join(item.message for item in report.findings)
            raise PatchError(f"Security review rejected patch: {messages}")
        repo_root = self._git(
            "rev-parse", "--show-toplevel", capture_output=True
        ).stdout.strip()
        if not repo_root:
            raise PatchError("The project is not inside a Git repository.")

        # Validate before changing branches or files.
        self._git("apply", "--check", "--unidiff-zero", "-", input_text=diff)
        if self.branch_name is None:
            self.branch_name = self._create_branch()
        self._git("apply", "--unidiff-zero", "-", input_text=diff)
        return self.branch_name

    def _create_branch(self) -> str:
        stem = datetime.now().strftime("breakmycode/fixes-%Y%m%d-%H%M%S")
        branch = stem
        suffix = 2
        while self._branch_exists(branch):
            branch = f"{stem}-{suffix}"
            suffix += 1
        self._git("switch", "-c", branch)
        return branch

    def _branch_exists(self, branch: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.project,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _git(
        self,
        *args: str,
        input_text: str | None = None,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=self.project,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or f"git {' '.join(args)} failed"
            raise PatchError(message)
        return result

