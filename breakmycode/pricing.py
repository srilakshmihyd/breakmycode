from __future__ import annotations

import json
import os


# USD per one million input/output tokens. Override the complete map with
# BREAKMYCODE_MODEL_PRICING_JSON when catalog pricing changes.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
    "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
}


def pricing_table() -> dict[str, tuple[float, float]]:
    configured = os.environ.get("BREAKMYCODE_MODEL_PRICING_JSON")
    if not configured:
        return DEFAULT_PRICING.copy()
    raw = json.loads(configured)
    return {
        model: (float(rates[0]), float(rates[1]))
        for model, rates in raw.items()
    }


def estimate_cost_usd(
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    rates = pricing_table().get(model)
    if rates is None or prompt_tokens is None or completion_tokens is None:
        return None
    return (prompt_tokens * rates[0] + completion_tokens * rates[1]) / 1_000_000

