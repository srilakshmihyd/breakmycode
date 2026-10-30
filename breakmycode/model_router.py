from __future__ import annotations

from dataclasses import dataclass
import os


DEFAULT_NEMOTRON_MODEL = "nvidia/Nemotron-3_5-Lightning"


@dataclass(frozen=True)
class ModelRouter:
    """Select a cost-appropriate Token Factory model for each agent task."""

    fast: str
    standard: str
    reasoning: str

    @classmethod
    def from_environment(cls) -> "ModelRouter":
        fallback = os.environ.get("NEBIUS_MODEL", DEFAULT_NEMOTRON_MODEL)
        return cls(
            fast=os.environ.get("NEBIUS_MODEL_FAST", fallback),
            standard=os.environ.get("NEBIUS_MODEL_STANDARD", fallback),
            reasoning=os.environ.get("NEBIUS_MODEL_REASONING", fallback),
        )

    def for_task(self, task: str, confidence: float = 1.0) -> str:
        if task in {"deep_diagnosis", "complex_review"} or confidence < 0.65:
            return self.reasoning
        if task in {"patch", "review", "test_generation"}:
            return self.standard
        return self.fast

