from types import SimpleNamespace
from unittest.mock import Mock

from breakmycode.llm import LLMAnalyzer, _strip_diff_fence


def _response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_mocked_llm_classifies_code_bug_and_requests_diff():
    client = Mock()
    client.chat.completions.create.side_effect = [
        _response(
            '{"verdict":"code_bug","explanation":"The set discards order."}'
        ),
        _response(
            "--- a/cart.py\n+++ b/cart.py\n@@ -1 +1 @@\n-old\n+new\n"
        ),
    ]
    analyzer = LLMAnalyzer(client=client, model="mock-model")

    result = analyzer.analyze(
        "test_cart.py::test_first",
        "def test_first(): ...",
        "def first_item(cart): ...",
        "AssertionError",
        True,
    )

    assert result.verdict == "code_bug"
    assert result.explanation == "The set discards order."
    assert result.diff.startswith("--- a/cart.py")
    assert client.chat.completions.create.call_count == 2
    first_prompt = client.chat.completions.create.call_args_list[0].kwargs[
        "messages"
    ][1]["content"]
    assert "def test_first(): ..." in first_prompt
    assert "def first_item(cart): ..." in first_prompt
    assert "AssertionError" in first_prompt


def test_model_diff_hunk_counts_are_normalized():
    malformed = """--- a/client.py
+++ b/client.py
@@ -1,4 +1,4 @@
 def health_status():
     \"\"\"Return status.\"\"\"
-    return 500
+    return 200
"""

    normalized = _strip_diff_fence(malformed)

    assert "@@ -1,3 +1,3 @@" in normalized


def test_mocked_llm_generates_new_pytest_file():
    client = Mock()
    client.chat.completions.create.return_value = _response(
        "--- /dev/null\n+++ b/tests/test_generated_cart.py\n"
        "@@ -0,0 +1,2 @@\n+def test_total():\n+    assert 1 + 1 == 2\n"
    )
    analyzer = LLMAnalyzer(client=client, model="mock-model")

    diff = analyzer.generate_tests(
        "### cart.py\ndef total(items): return sum(items)",
        "(no existing pytest files found)",
    )

    assert "+++ b/tests/test_generated_cart.py" in diff
    prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "brand-new Python files" in prompt
    assert "def total" in prompt

