"""GC tasks for cleaning up various resources."""

from app.services.gc.tasks.expired_sandbox import ExpiredSandboxGC
from app.services.gc.tasks.idle_session import IdleSessionGC
from app.services.gc.tasks.orphan_container import OrphanContainerGC
from app.services.gc.tasks.orphan_cargo import OrphanCargoGC

__all__ = [
    "IdleSessionGC",
    "ExpiredSandboxGC",
    "OrphanCargoGC",
    "OrphanContainerGC",
]
