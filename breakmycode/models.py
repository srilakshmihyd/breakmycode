from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TestRunResult:
    outcomes: dict[str, str]
    failures: dict[str, str] = field(default_factory=dict)
    pytest_output: str = ""


@dataclass(frozen=True)
class TestStats:
    test_name: str
    passes: int
    failures: int
    total_runs: int
    failure_outputs: tuple[str, ...] = ()

    @property
    def is_flaky(self) -> bool:
        return self.passes > 0 and self.failures > 0

    @property
    def failure_rate(self) -> float:
        if self.total_runs == 0:
            return 0.0
        return self.failures / self.total_runs

    @property
    def summary(self) -> str:
        percent = self.failure_rate * 100
        rendered = f"{percent:.1f}".rstrip("0").rstrip(".")
        return f"failed {self.failures}/{self.total_runs} ({rendered}%)"


@dataclass(frozen=True)
class Analysis:
    verdict: str
    explanation: str
    diff: str | None = None
    confidence: float = 0.0
    model: str = ""


@dataclass(frozen=True)
class PatchReview:
    safe: bool
    explanation: str
    risks: tuple[str, ...] = ()
    model: str = ""

