from cart import first_item


def test_first_item_preserves_cart_order():
    assert first_item(["apple", "pear", "fig"]) == "apple"


def test_empty_cart_returns_none():
    assert first_item([]) is None

