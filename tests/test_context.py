from breakmycode.context import collect_source_context


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
