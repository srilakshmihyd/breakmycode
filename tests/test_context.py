from breakmycode.context import collect_generation_context, collect_source_context


def test_collects_test_and_locally_imported_code(tmp_path):
    (tmp_path / "cart.py").write_text(
        "def first_item(cart):\n    return cart[0]\n", encoding="utf-8"
    )
    (tmp_path / "test_cart.py").write_text(
        "from cart import first_item\n\ndef test_first():\n    assert first_item([1]) == 1\n",
        encoding="utf-8",
    )

    test_source, code_under_test = collect_source_context(
        tmp_path, "test_cart.py::test_first"
    )

    assert "def test_first" in test_source
    assert "### cart.py" in code_under_test
    assert "def first_item" in code_under_test


def test_generation_context_separates_source_tests_and_secrets(tmp_path):
    (tmp_path / "cart.py").write_text("def total(items):\n    return sum(items)\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_cart.py").write_text("def test_total():\n    assert True\n")
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=must-not-appear\n")

    source_context, test_context = collect_generation_context(tmp_path)

    assert "### cart.py" in source_context
    assert "### tests/test_cart.py" in test_context
    assert "must-not-appear" not in source_context + test_context
