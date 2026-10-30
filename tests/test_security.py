from breakmycode.redteam import run_red_team
from breakmycode.security import (
    generated_test_paths,
    redact_untrusted_text,
    repair_unified_diff,
    review_patch,
    review_test_patch,
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


def test_new_generated_pytest_file_passes_policy(tmp_path):
    diff = """--- /dev/null
+++ b/tests/test_generated_cart.py
@@ -0,0 +1,2 @@
+def test_total():
+    assert 1 + 1 == 2
"""

    assert review_test_patch(tmp_path, diff).safe is True
    assert generated_test_paths(diff) == ("tests/test_generated_cart.py",)


def test_generated_tests_cannot_modify_production_or_existing_tests(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_cart.py").write_text("def test_old():\n    assert True\n")
    diff = """--- a/tests/test_cart.py
+++ b/tests/test_cart.py
@@ -1,2 +1,2 @@
 def test_old():
-    assert True
+    assert False
"""

    report = review_test_patch(tmp_path, diff)

    assert report.safe is False
    assert "existing_test_modification" in {item.rule for item in report.findings}


def test_generated_tests_block_shell_network_and_environment_access(tmp_path):
    diff = """--- /dev/null
+++ b/tests/test_generated_unsafe.py
@@ -0,0 +1,6 @@
+import os
+import requests
+def test_unsafe():
+    os.getenv("API_KEY")
+    requests.get("https://example.com")
+    assert True
"""

    report = review_test_patch(tmp_path, diff)
    rules = {item.rule for item in report.findings}

    assert report.safe is False
    assert {"network_access", "environment_access"}.issubset(rules)


def test_generated_tests_must_be_valid_python(tmp_path):
    diff = """--- /dev/null
+++ b/tests/test_generated_invalid.py
@@ -0,0 +1,2 @@
+def test_broken(:
+    assert True
"""

    report = review_test_patch(tmp_path, diff)

    assert report.safe is False
    assert "invalid_python" in {item.rule for item in report.findings}


def test_generated_tests_are_bounded(tmp_path):
    functions = "\n".join(
        f"def test_case_{index}():\n    assert True" for index in range(6)
    )
    added = "\n".join("+" + line for line in functions.splitlines())
    diff = (
        "--- /dev/null\n+++ b/tests/test_generated_many.py\n"
        f"@@ -0,0 +1,12 @@\n{added}\n"
    )

    report = review_test_patch(tmp_path, diff)

    assert report.safe is False
    assert "generated_test_count" in {item.rule for item in report.findings}

