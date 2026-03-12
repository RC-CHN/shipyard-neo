"""Extraction strategy interfaces and shared data structures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


def compute_payload_hash(*, steps: list[dict[str, Any]]) -> str:
    """Compute a deterministic hash from the ordered command sequence."""
    commands = [str(step.get("cmd", "")).strip() for step in steps]
    canonical = json.dumps(commands, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class VariableSpec:
    """Variable declaration for a parameterizable command argument."""

    type: str
    default_value: Any
    action_index: int
    arg_position: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "default_value": self.default_value,
            "action_index": self.action_index,
            "arg_position": self.arg_position,
        }


@dataclass(slots=True)
class ExtractionContext:
    """Execution context passed into extraction strategies."""

    owner: str
    execution_id: str
    sandbox_id: str | None
    code: str
    description: str | None
    tags: str | None


@dataclass(slots=True)
class ExtractionResult:
    """Structured extraction output consumed by the learning pipeline."""

    skill_key: str
    steps: list[dict[str, Any]]
    description: str | None = None
    variables: dict[str, VariableSpec] | None = None
    payload_hash: str | None = None

    def __post_init__(self) -> None:
        if self.payload_hash is None:
            self.payload_hash = compute_payload_hash(steps=self.steps)

    def variables_payload(self) -> dict[str, dict[str, Any]] | None:
        if not self.variables:
            return None
        return {name: spec.to_payload() for name, spec in self.variables.items()}


@runtime_checkable
class ExtractionStrategy(Protocol):
    """Extract structured candidate results from actionable segments."""

    async def extract(
        self,
        *,
        segments: list[list[dict[str, Any]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]: ...

