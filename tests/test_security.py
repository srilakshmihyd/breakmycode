from breakmycode.redteam import run_red_team
from breakmycode.security import (
    redact_untrusted_text,
    repair_unified_diff,
    review_patch,
)


def test_red_team_attacks_are_blocked(tmp_path):
    results = run_red_team(tmp_path)

    assert len(results) == 4
    assert all(result.blocked for result in results)


def test_safe_production_patch_passes_policy(tmp_path):
    (tmp_path / "cart.py").write_text("return old\n", encoding="utf-8")
    diff = """--- a/cart.py
+++ b/cart.py
@@ -1 +1 @@
-return old
+return new
"""

    assert review_patch(tmp_path, diff).safe is True


def test_secret_is_redacted_before_model_context():
    text = "NEBIUS_API_KEY=very-secret-value"

    redacted = redact_untrusted_text(text)

    assert "very-secret-value" not in redacted
    assert "[REDACTED]" in redacted


def test_bare_model_hunk_is_repaired_from_file_context(tmp_path):
    (tmp_path / "client.py").write_text(
        "def status():\n    return 500\n", encoding="utf-8"
    )
    malformed = """--- a/client.py
+++ b/client.py
@@
 def status():
-    return 500
+    return 200
"""

    repaired = repair_unified_diff(tmp_path, malformed)

    assert "@@ -1,2 +1,2 @@" in repaired

