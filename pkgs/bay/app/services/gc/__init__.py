"""GC (Garbage Collection) service for Bay.

This module provides background garbage collection for:
- Idle sessions (IdleSessionGC)
- Expired sandboxes (ExpiredSandboxGC)
- Orphan workspaces (OrphanWorkspaceGC)
- Orphan containers (OrphanContainerGC)

Usage:
    from app.services.gc import GCScheduler
    
    scheduler = GCScheduler(...)
    await scheduler.start()
"""

from app.services.gc.base import GCResult, GCTask
from app.services.gc.coordinator import GCCoordinator, NoopCoordinator
from app.services.gc.scheduler import GCScheduler

__all__ = [
    "GCTask",
    "GCResult",
    "GCCoordinator",
    "NoopCoordinator",
    "GCScheduler",
]
