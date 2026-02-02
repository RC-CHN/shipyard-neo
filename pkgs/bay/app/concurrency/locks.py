"""Sandbox-level in-memory locks for concurrency control.

This module provides sandbox-level locks used by:
- SandboxManager (ensure_running, stop, delete)
- GC tasks (IdleSessionGC, ExpiredSandboxGC)

Note: These locks only work within a single process/instance.
For multi-instance deployments, DB-level locking (FOR UPDATE) is also used.
Phase 2 will introduce distributed coordination via GCCoordinator.
"""

from __future__ import annotations

import asyncio

# Lock map for sandbox-level concurrency control (single-instance only)
# Key: sandbox_id, Value: asyncio.Lock
_sandbox_locks: dict[str, asyncio.Lock] = {}
_sandbox_locks_lock = asyncio.Lock()


async def get_sandbox_lock(sandbox_id: str) -> asyncio.Lock:
    """Get or create a lock for a specific sandbox.

    This ensures concurrent operations on the same sandbox are serialized,
    preventing race conditions between:
    - Multiple ensure_running calls
    - ensure_running and GC tasks
    - stop/delete and GC tasks

    Args:
        sandbox_id: The sandbox ID to get lock for

    Returns:
        asyncio.Lock for the specified sandbox
    """
    async with _sandbox_locks_lock:
        if sandbox_id not in _sandbox_locks:
            _sandbox_locks[sandbox_id] = asyncio.Lock()
        return _sandbox_locks[sandbox_id]


async def cleanup_sandbox_lock(sandbox_id: str) -> None:
    """Cleanup lock for a deleted sandbox.

    Called after sandbox deletion to free memory.

    Args:
        sandbox_id: The sandbox ID to cleanup lock for
    """
    async with _sandbox_locks_lock:
        _sandbox_locks.pop(sandbox_id, None)


async def cleanup_deleted_sandbox_locks(deleted_sandbox_ids: set[str]) -> None:
    """Cleanup locks for multiple deleted sandboxes.

    Called by GC scheduler after each cycle to clean up stale locks.

    Args:
        deleted_sandbox_ids: Set of sandbox IDs that have been deleted
    """
    async with _sandbox_locks_lock:
        for sandbox_id in deleted_sandbox_ids:
            _sandbox_locks.pop(sandbox_id, None)


def get_lock_count() -> int:
    """Get current number of locks (for testing/metrics)."""
    return len(_sandbox_locks)
