"""OrphanContainerGC - Clean up orphan containers (Strict mode).

SAFETY FIRST: This task only deletes containers that:
1. Have name prefix "bay-session-"
2. Have ALL required labels (bay.session_id, bay.sandbox_id, etc.)
3. Have bay.managed="true"
4. Have bay.instance_id matching the configured gc.instance_id
5. Have a bay.session_id that does NOT exist in the database

This strict mode prevents accidental deletion of user containers.
DEFAULT: Disabled (must be explicitly enabled in config).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import GCConfig
from app.models.session import Session
from app.services.gc.base import GCResult, GCTask

if TYPE_CHECKING:
    from app.drivers.base import Driver

logger = structlog.get_logger()

# Required labels for strict mode identification
REQUIRED_LABELS = [
    "bay.session_id",
    "bay.sandbox_id",
    "bay.workspace_id",
    "bay.instance_id",
    "bay.managed",
]

# Container name prefix for Bay-managed containers
CONTAINER_NAME_PREFIX = "bay-session-"


class OrphanContainerGC(GCTask):
    """GC task for cleaning up orphan containers (Strict mode).

    This task discovers containers that may belong to Bay but have no
    corresponding DB record, indicating they are orphaned due to
    crashes, restarts, or incomplete cleanup.

    STRICT MODE (default and only mode):
        Only containers meeting ALL of the following criteria are deleted:
        1. Name starts with "bay-session-"
        2. Has all required labels
        3. bay.managed = "true"
        4. bay.instance_id = configured gc.instance_id
        5. bay.session_id not found in DB

    Any container not meeting ALL criteria is SKIPPED (logged only).
    """

    def __init__(
        self,
        driver: "Driver",
        db_session: AsyncSession,
        gc_config: GCConfig,
    ) -> None:
        self._driver = driver
        self._db = db_session
        self._gc_config = gc_config
        self._log = logger.bind(gc_task="orphan_container")
        self._instance_id = gc_config.get_instance_id()

    @property
    def name(self) -> str:
        return "orphan_container"

    async def run(self) -> GCResult:
        """Execute orphan container cleanup."""
        result = GCResult(task_name=self.name)

        # Discovery: list all containers with bay.managed=true and matching instance_id
        filter_labels = {
            "bay.managed": "true",
            "bay.instance_id": self._instance_id,
        }

        self._log.info(
            "gc.orphan_container.discovery.start",
            instance_id=self._instance_id,
            filter_labels=filter_labels,
        )

        instances = await self._driver.list_runtime_instances(labels=filter_labels)

        self._log.info(
            "gc.orphan_container.discovery.complete",
            count=len(instances),
        )

        for instance in instances:
            try:
                cleaned = await self._process_instance(instance, result)
                if cleaned:
                    result.cleaned_count += 1
            except Exception as e:
                self._log.exception(
                    "gc.orphan_container.item_error",
                    instance_id=instance.id,
                    instance_name=instance.name,
                    error=str(e),
                )
                result.add_error(f"container {instance.id}: {e}")

        return result

    async def _process_instance(self, instance, result: GCResult) -> bool:
        """Process a single container instance.

        Returns True if deleted, False if skipped.
        """
        # Validation step 1: Check name prefix
        if not instance.name.startswith(CONTAINER_NAME_PREFIX):
            self._log.debug(
                "gc.orphan_container.skip.name_prefix",
                instance_id=instance.id,
                instance_name=instance.name,
                reason="name does not start with bay-session-",
            )
            result.skipped_count += 1
            return False

        # Validation step 2: Check all required labels exist
        missing_labels = []
        for label in REQUIRED_LABELS:
            if label not in instance.labels:
                missing_labels.append(label)

        if missing_labels:
            self._log.debug(
                "gc.orphan_container.skip.missing_labels",
                instance_id=instance.id,
                instance_name=instance.name,
                missing_labels=missing_labels,
            )
            result.skipped_count += 1
            return False

        # Validation step 3: Check bay.managed = "true" (already filtered, but double-check)
        if instance.labels.get("bay.managed") != "true":
            self._log.debug(
                "gc.orphan_container.skip.not_managed",
                instance_id=instance.id,
                instance_name=instance.name,
            )
            result.skipped_count += 1
            return False

        # Validation step 4: Check bay.instance_id matches (already filtered, but double-check)
        container_instance_id = instance.labels.get("bay.instance_id")
        if container_instance_id != self._instance_id:
            self._log.warning(
                "gc.orphan_container.skip.instance_mismatch",
                instance_id=instance.id,
                instance_name=instance.name,
                container_instance_id=container_instance_id,
                expected_instance_id=self._instance_id,
            )
            result.skipped_count += 1
            return False

        # Validation step 5: Check if session exists in DB
        session_id = instance.labels.get("bay.session_id")
        if not session_id:
            self._log.warning(
                "gc.orphan_container.skip.no_session_id",
                instance_id=instance.id,
                instance_name=instance.name,
            )
            result.skipped_count += 1
            return False

        # Query DB to check if session exists
        db_result = await self._db.execute(
            select(Session.id).where(Session.id == session_id)
        )
        session_exists = db_result.scalars().first() is not None

        if session_exists:
            # Not an orphan - session record exists
            self._log.debug(
                "gc.orphan_container.skip.session_exists",
                instance_id=instance.id,
                instance_name=instance.name,
                session_id=session_id,
            )
            result.skipped_count += 1
            return False

        # All validations passed - this is a true orphan, delete it
        self._log.info(
            "gc.orphan_container.deleting",
            instance_id=instance.id,
            instance_name=instance.name,
            session_id=session_id,
            sandbox_id=instance.labels.get("bay.sandbox_id"),
        )

        await self._driver.destroy_runtime_instance(instance.id)

        self._log.info(
            "gc.orphan_container.deleted",
            instance_id=instance.id,
            instance_name=instance.name,
            session_id=session_id,
        )

        return True
