"""Request-scoped wall-clock budget shared by expensive pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable


class BudgetExhaustedError(RuntimeError):
    """Internal control signal for a stage that lost a budget-start race."""


@dataclass(slots=True)
class WallClockBudget:
    """Monotonic budget which records the first stage that exhausts it."""

    total_seconds: float
    clock: Callable[[], float] = time.monotonic
    _started: float = field(init=False, repr=False)
    exhausted_stage: str | None = field(default=None, init=False)
    exhaustion_reason: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.total_seconds <= 0:
            raise ValueError("wall-clock budget must be positive")
        self.total_seconds = float(self.total_seconds)
        self._started = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self._started)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed_seconds)

    @property
    def exhausted(self) -> bool:
        return self.remaining_seconds <= 0.0

    def mark_exhausted(self, stage: str, reason: str | None = None) -> None:
        if self.exhausted_stage is None:
            self.exhausted_stage = stage
            self.exhaustion_reason = reason or (
                f"Workflow wall-clock budget exhausted during {stage}."
            )

    def can_start(self, stage: str, *, minimum_seconds: float = 0.1) -> bool:
        if self.remaining_seconds >= minimum_seconds:
            return True
        self.mark_exhausted(
            stage,
            f"Insufficient workflow budget to start {stage}: "
            f"{self.remaining_seconds:.3f}s remaining.",
        )
        return False

    def timeout_for(
        self,
        maximum_seconds: float,
        stage: str,
        *,
        minimum_seconds: float = 0.1,
    ) -> float | None:
        if not self.can_start(stage, minimum_seconds=minimum_seconds):
            return None
        return min(float(maximum_seconds), self.remaining_seconds)

    def snapshot(self) -> dict[str, object]:
        return {
            "total_seconds": self.total_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "remaining_seconds": round(self.remaining_seconds, 6),
            "exhausted": self.exhausted_stage is not None or self.exhausted,
            "exhausted_stage": self.exhausted_stage,
            "exhaustion_reason": self.exhaustion_reason,
        }
