"""Unit tests for MetaPromptService (Phase 2).

Tests cover:
- seed_defaults: creates default prompts when DB is empty, idempotent on re-run
- sample_prompt: returns a prompt from the archive
- record_usage / record_success: updates counters
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.services.skills.evolution.meta_prompt import MetaPromptService


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def svc(db_session: AsyncSession) -> MetaPromptService:
    return MetaPromptService(db_session)


class TestSeedDefaults:
    async def test_creates_default_prompts_on_empty_db(self, svc: MetaPromptService):
        count = await svc.seed_defaults()

        assert count > 0

    async def test_each_default_prompt_has_instruction_text(self, svc: MetaPromptService):
        await svc.seed_defaults()
        prompts = await svc.list_all()

        assert len(prompts) > 0
        for p in prompts:
            assert p.instruction.strip() != ""

    async def test_default_prompts_are_flagged_is_default(self, svc: MetaPromptService):
        await svc.seed_defaults()
        prompts = await svc.list_all()

        assert all(p.is_default for p in prompts)

    async def test_seed_is_idempotent(self, svc: MetaPromptService):
        count_first = await svc.seed_defaults()
        count_second = await svc.seed_defaults()

        assert count_first == count_second
        # Total number of prompts should not have doubled
        all_prompts = await svc.list_all()
        assert len(all_prompts) == count_first


class TestSamplePrompt:
    async def test_sample_returns_a_prompt_after_seeding(self, svc: MetaPromptService):
        await svc.seed_defaults()

        prompt = await svc.sample_prompt()

        assert prompt is not None
        assert prompt.instruction.strip() != ""

    async def test_sample_returns_none_when_empty(self, svc: MetaPromptService):
        prompt = await svc.sample_prompt()

        assert prompt is None


class TestRecordUsage:
    async def test_increments_usage_count(self, svc: MetaPromptService):
        await svc.seed_defaults()
        prompts = await svc.list_all()
        prompt = prompts[0]
        assert prompt.usage_count == 0

        await svc.record_usage(prompt.id)

        updated = await svc.get_prompt(prompt.id)
        assert updated is not None
        assert updated.usage_count == 1

    async def test_multiple_usages_accumulate(self, svc: MetaPromptService):
        await svc.seed_defaults()
        prompt = (await svc.list_all())[0]

        await svc.record_usage(prompt.id)
        await svc.record_usage(prompt.id)
        await svc.record_usage(prompt.id)

        updated = await svc.get_prompt(prompt.id)
        assert updated.usage_count == 3


class TestRecordSuccess:
    async def test_increments_success_count(self, svc: MetaPromptService):
        await svc.seed_defaults()
        prompt = (await svc.list_all())[0]
        assert prompt.success_count == 0

        await svc.record_success(prompt.id)

        updated = await svc.get_prompt(prompt.id)
        assert updated.success_count == 1

    async def test_success_count_independent_from_usage_count(self, svc: MetaPromptService):
        await svc.seed_defaults()
        prompt = (await svc.list_all())[0]

        await svc.record_usage(prompt.id)
        await svc.record_usage(prompt.id)
        await svc.record_success(prompt.id)

        updated = await svc.get_prompt(prompt.id)
        assert updated.usage_count == 2
        assert updated.success_count == 1
