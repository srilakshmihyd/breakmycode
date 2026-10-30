import subprocess

from breakmycode.patches import PatchApplier


def _git(project, *args):
    return subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def test_approved_patch_is_applied_on_a_new_branch(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "cart.py").write_text(
        "def first_item(cart):\n    return list(set(cart))[0]\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "cart.py")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    _git(tmp_path, "branch", "-M", "main")
    diff = """--- a/cart.py
+++ b/cart.py
@@ -1,2 +1,2 @@
 def first_item(cart):
-    return list(set(cart))[0]
+    return cart[0] if cart else None
"""

    branch = PatchApplier(tmp_path).apply(diff)

    assert branch.startswith("breakmycode/fixes-")
    assert _git(tmp_path, "branch", "--show-current") == branch
    assert "return cart[0] if cart else None" in (tmp_path / "cart.py").read_text(
        encoding="utf-8"
    )
