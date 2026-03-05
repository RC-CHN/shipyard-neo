"""MetaPromptService — manages the archive of mutation instructions."""

from __future__ import annotations

import random
import uuid
from typing import Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.skill import MetaPrompt
from app.utils.datetime import utcnow

logger = structlog.get_logger()

# Default mutation instructions seeded on first run.
# Each is a distinct strategy for improving a skill.
DEFAULT_INSTRUCTIONS: list[str] = [
    (
        "Make the instructions clearer and more explicit. "
        "Describe each step in plain language a non-expert could follow. "
        "Add concrete examples of what to look for on the page."
    ),
    (
        "Based on the failure context, identify what went wrong and add explicit "
        "error-handling or fallback steps. "
        "For example: if an element is not found, suggest alternative selectors or wait strategies."
    ),
    (
        "Simplify the instructions. Remove any unnecessary steps. "
        "Make each action more direct and focused on the essential outcome."
    ),
    (
        "Add more specific selector strategies and fallback approaches for UI elements "
        "that may have changed due to a site redesign. "
        "Prefer semantic selectors (role, label, text) over brittle CSS class selectors."
    ),
    (
        "Rewrite the instructions from the perspective of a human expert who has seen "
        "the naive approach fail. What would they do differently? "
        "Focus on robustness and handling of dynamic or delayed content."
    ),
]


class MetaPromptService:
    """Service for managing the MetaPrompt archive."""

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session
        self._log = logger.bind(component="meta_prompt_service")

    async def seed_defaults(self) -> int:
        """Seed default instructions if none exist. Returns count of default prompts."""
        result = await self._db.execute(
            select(MetaPrompt).where(MetaPrompt.is_default.is_(True))
        )
        existing = result.scalars().all()
        if existing:
            return len(existing)

        now = utcnow()
        for instruction in DEFAULT_INSTRUCTIONS:
            prompt = MetaPrompt(
                id=f"mp-{uuid.uuid4().hex[:12]}",
                instruction=instruction,
                is_default=True,
                usage_count=0,
                success_count=0,
                created_at=now,
                updated_at=now,
            )
            self._db.add(prompt)

        await self._db.commit()
        self._log.info("meta_prompt.seeded_defaults", count=len(DEFAULT_INSTRUCTIONS))
        return len(DEFAULT_INSTRUCTIONS)

    async def list_all(self) -> Sequence[MetaPrompt]:
        result = await self._db.execute(select(MetaPrompt))
        return result.scalars().all()

    async def get_prompt(self, prompt_id: str) -> MetaPrompt | None:
        result = await self._db.execute(
            select(MetaPrompt).where(MetaPrompt.id == prompt_id)
        )
        return result.scalars().first()

    async def sample_prompt(self) -> MetaPrompt | None:
        """Sample a prompt weighted by success rate.

        Weight = (success_count + 1) / (usage_count + 2)
        This gives new/unused prompts a fair chance while rewarding effective ones.
        """
        result = await self._db.execute(select(MetaPrompt))
        prompts = result.scalars().all()
        if not prompts:
            return None

        weights = [
            (p.success_count + 1) / (p.usage_count + 2) for p in prompts
        ]
        return random.choices(prompts, weights=weights, k=1)[0]

    async def record_usage(self, prompt_id: str) -> None:
        prompt = await self.get_prompt(prompt_id)
        if prompt is None:
            return
        prompt.usage_count += 1
        prompt.updated_at = utcnow()
        self._db.add(prompt)
        await self._db.commit()

    async def record_success(self, prompt_id: str) -> None:
        prompt = await self.get_prompt(prompt_id)
        if prompt is None:
            return
        prompt.success_count += 1
        prompt.updated_at = utcnow()
        self._db.add(prompt)
        await self._db.commit()
