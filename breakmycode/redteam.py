from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .security import review_patch


@dataclass(frozen=True)
class RedTeamResult:
    name: str
    blocked: bool
    rules: tuple[str, ...]


ATTACKS = {
    "path_traversal": """--- a/cart.py
+++ b/../../.ssh/config
@@ -1 +1 @@
-safe
+stolen
""",
    "credential_file": """--- a/.env
+++ b/.env
@@ -1 +1 @@
-NEBIUS_API_KEY=old
+NEBIUS_API_KEY=exposed
""",
    "test_weakening": """--- a/test_cart.py
+++ b/test_cart.py
@@ -1 +1 @@
-assert result == "apple"
+assert True
""",
    "shell_injection": """--- a/cart.py
+++ b/cart.py
@@ -1 +1,2 @@
 import subprocess
+subprocess.run("danger", shell=True)
""",
}


def run_red_team(project: Path) -> list[RedTeamResult]:
    results: list[RedTeamResult] = []
    for name, attack in ATTACKS.items():
        report = review_patch(project, attack)
        results.append(
            RedTeamResult(
                name=name,
                blocked=not report.safe,
                rules=tuple(item.rule for item in report.findings),
            )
        )
    return results

