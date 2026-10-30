def unstable_clock():
    """Incorrectly derive a clock tick from Python's randomized hash seed."""
    return hash("unstable-clock") % 2

