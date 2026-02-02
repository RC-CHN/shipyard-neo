"""GC coordination for multi-instance deployments.

Phase 1.5: NoopCoordinator (always allow execution)
Phase 2+: DbLeaseCoordinator (leader election via DB lease)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncIterator


class GCCoordinator(ABC):
    """Abstract coordinator for GC execution.

    Coordinators control whether a GC cycle should run on this instance.
    This is used for multi-instance deployments to prevent duplicate
    GC execution across instances.

    Phase 1.5: Single-instance deployment, use NoopCoordinator.
    Phase 2: Multi-instance deployment, use DbLeaseCoordinator.
    """

    @abstractmethod
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        """Attempt to acquire coordination lock.

        Usage:
            async with coordinator.acquire() as acquired:
                if acquired:
                    # Run GC tasks
                    ...

        Yields:
            True if lock acquired, False if another instance holds the lock
        """
        ...


class NoopCoordinator(GCCoordinator):
    """No-op coordinator that always allows execution.

    Used for single-instance deployments where no coordination is needed.
    """

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        """Always yield True (no coordination)."""
        yield True
