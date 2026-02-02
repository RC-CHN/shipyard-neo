"""GC task base classes and result structures."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class GCResult:
    """Result of a GC task execution.

    Attributes:
        task_name: Name of the GC task
        cleaned_count: Number of resources successfully cleaned
        skipped_count: Number of resources skipped (e.g., conditions not met)
        errors: List of error messages for failed cleanups
    """

    task_name: str = ""
    cleaned_count: int = 0
    skipped_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Whether the task completed without errors."""
        return len(self.errors) == 0

    def add_error(self, error: str) -> None:
        """Add an error message."""
        self.errors.append(error)


class GCTask(ABC):
    """Abstract base class for GC tasks.

    Each GC task is responsible for cleaning up a specific type of resource:
    - IdleSessionGC: Reclaim compute for idle sandboxes
    - ExpiredSandboxGC: Delete sandboxes past their TTL
    - OrphanCargoGC: Clean up orphan managed cargos
    - OrphanContainerGC: Clean up orphan containers (strict mode)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the task name (for logging and metrics)."""
        ...

    @abstractmethod
    async def run(self) -> GCResult:
        """Execute the GC task.

        This method should:
        1. Query for resources matching the cleanup criteria
        2. For each resource, attempt cleanup with proper error handling
        3. Return a GCResult summarizing the cleanup

        Individual resource cleanup failures should be logged but not
        abort the entire task. Errors should be collected in GCResult.errors.

        Returns:
            GCResult with cleanup statistics
        """
        ...
