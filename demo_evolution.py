#!/usr/bin/env python3
"""
Skill Evolution Demo
====================
Demonstrates the full evolution pipeline end-to-end:

1. Declares skill goals (human intent only — "I want a skill that does X")
2. Seeds initial skill candidates with summaries and instructions
3. Reports failure outcomes (the evolution signal feed)
4. Runs the LLM mutation cycle using Claude (or mock if no API key)
5. Shows the evolved skill candidates and their reasoning

Run from pkgs/bay directory using Bay's venv:
    .venv/bin/python ../../demo_evolution.py

Or set ANTHROPIC_API_KEY for real Claude mutations:
    ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python ../../demo_evolution.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.config import EvolutionConfig, LlmEvolutionConfig
from app.models.skill import (
    ExecutionType,
    MetaPrompt,
    SkillCandidate,
    SkillGoal,
    SkillOutcome,
    SkillReleaseStage,
)
from app.services.skills import SkillLifecycleService
from app.services.skills.evolution.agent import SkillMutationAgent
from app.services.skills.evolution.llm import LlmEvolutionClient, MutationOutput
from app.services.skills.evolution.meta_prompt import MetaPromptService
from app.services.skills.evolution.scheduler import EvolutionCycleResult, EvolutionScheduler

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))  # WARNING+ only

# ---------------------------------------------------------------------------
# Skills we want to evolve (human intent only)
# ---------------------------------------------------------------------------

SKILLS = [
    {
        "skill_key": "github-get-stars",
        "goal": "Given a GitHub repo URL, return the exact current star count as an integer.",
        "summary": "Navigate to the GitHub repo page and read the star count element.",
        "usage_notes": (
            "Open the repo URL in a browser. "
            "Look for the star count near the top of the page and return it as an integer."
        ),
        "preconditions": {
            "input": "A valid GitHub repo URL",
            "network": "GitHub is accessible",
        },
        "postconditions": {
            "output": "Integer star count",
        },
        "failures": [
            "Element not found at selector '.social-count'. Page may have loaded differently.",
            "Star count returned None — element existed but had no text content after JS render.",
            "Timeout waiting for page load after 3s. No retry logic was applied.",
        ],
    },
    {
        "skill_key": "hacker-news-top10",
        "goal": "Fetch the titles and URLs of the current top 10 stories on Hacker News.",
        "summary": "Navigate to news.ycombinator.com and scrape the top 10 story titles with links.",
        "usage_notes": (
            "Open the HN homepage. "
            "Extract the first 10 .titleline elements and return their text and href attributes."
        ),
        "preconditions": {
            "browser": "Available with JavaScript enabled",
            "network": "news.ycombinator.com is accessible",
        },
        "postconditions": {
            "output": "List of {title, url} dicts with exactly 10 items",
        },
        "failures": [
            "Only got 8 items — the page structure may have changed. .titleline missed 2 rows.",
            "URLs were relative (item?id=...) instead of absolute. Needed to resolve base URL.",
            "Title text included vote/rank numbers. Parsing logic not robust enough.",
        ],
    },
    {
        "skill_key": "pypi-package-info",
        "goal": "Given a PyPI package name, return its latest version and one-line description.",
        "summary": "Call the PyPI JSON API to fetch package metadata.",
        "usage_notes": (
            "Make a GET request to https://pypi.org/pypi/{package}/json. "
            "Extract info.version and info.summary from the response."
        ),
        "preconditions": {
            "input": "A valid PyPI package name (str)",
            "network": "pypi.org is accessible",
        },
        "postconditions": {
            "output": "Dict with keys: version (str), summary (str)",
        },
        "failures": [
            "KeyError on info.summary for packages that set it to None. Need null check.",
            "HTTP 404 for yanked packages — no fallback to previous version attempted.",
            "Rate limiting from PyPI (429) not handled; retried immediately and failed again.",
        ],
    },
]

# ---------------------------------------------------------------------------
# Anthropic LLM mutation client
# ---------------------------------------------------------------------------

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

_MUTATION_SYSTEM = """\
You are an expert at improving AI agent skill instructions.
You receive:
1. The current skill content (summary, usage notes, pre/postconditions)
2. Recent failure reports from executions of this skill
3. A mutation strategy to apply

Your task: rewrite the skill content to address the failures, applying the given strategy.
Return strict JSON only. The mutation_reasoning field MUST explain specifically what you changed and why.
No markdown, no explanation outside the JSON object.\
"""

_MUTATION_SCHEMA = {
    "type": "object",
    "required": ["summary", "usage_notes", "preconditions", "postconditions", "mutation_reasoning"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "usage_notes": {"type": "string"},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "postconditions": {"type": "array", "items": {"type": "string"}},
        "mutation_reasoning": {"type": "string"},
    },
}


class AnthropicMutationClient:
    """Calls Claude Haiku for skill mutation via tool_use for structured output."""

    async def generate_mutation(self, prompt: str) -> MutationOutput:
        if not ANTHROPIC_KEY:
            return _mock_mutation(prompt)

        async with httpx.AsyncClient(timeout=60) as ac:
            r = await ac.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "system": _MUTATION_SYSTEM,
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [
                        {
                            "name": "output_mutation",
                            "description": "Output the mutated skill content as structured data",
                            "input_schema": _MUTATION_SCHEMA,
                        }
                    ],
                    "tool_choice": {"type": "tool", "name": "output_mutation"},
                },
            )
            r.raise_for_status()
            data = r.json()
            for block in data["content"]:
                if block.get("type") == "tool_use" and block.get("name") == "output_mutation":
                    inp = block["input"]
                    return MutationOutput(
                        summary=inp["summary"],
                        usage_notes=inp["usage_notes"],
                        preconditions=inp["preconditions"],
                        postconditions=inp["postconditions"],
                        mutation_reasoning=inp["mutation_reasoning"],
                    )
        raise RuntimeError("No tool_use block in Claude response")


def _mock_mutation(prompt: str) -> MutationOutput:
    """Deterministic mock for demo without API key."""
    skill_line = next((l for l in prompt.split("\n") if "SKILL:" in l), "# SKILL: unknown")
    skill_key = skill_line.replace("# SKILL:", "").strip()
    return MutationOutput(
        summary=(
            f"[EVOLVED] {skill_key}: Use semantic selectors, explicit waits, and fallback strategies."
        ),
        usage_notes=(
            "1. Navigate to the target URL and wait for DOMContentLoaded (max 10s). "
            "2. Use role/aria/text selectors as primary; fall back to CSS class selectors. "
            "3. If element not found after 5s, scroll down and retry once. "
            "4. Validate the extracted value before returning — log and raise if None or empty."
        ),
        preconditions=[
            "Browser available with JavaScript enabled",
            "Target URL is accessible from the network",
            "Page renders within 10 seconds",
        ],
        postconditions=[
            "Returns a non-None, non-empty value of the expected type",
            "Value validated for plausibility before return",
        ],
        mutation_reasoning=(
            f"Original {skill_key} instructions used brittle CSS class selectors with no wait "
            "or fallback. Failures showed elements not found due to async rendering and selector "
            "changes. Changes: (1) switched to semantic selectors, (2) added explicit 5s wait, "
            "(3) added scroll-and-retry fallback, (4) added post-extraction validation."
        ),
    )


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------

async def run_demo() -> None:
    # Connect to Bay's live SQLite DB (same file Bay server uses)
    db_url = "sqlite+aiosqlite:///./bay.db"
    engine = create_async_engine(db_url, echo=False)

    # Ensure all tables exist (auto-migrate handles new columns via Bay startup)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    print("\n" + "=" * 64)
    print("  SKILL EVOLUTION DEMO  —  AI-native skill improvement loop")
    print("=" * 64)
    llm_label = "Claude Haiku (Anthropic API)" if ANTHROPIC_KEY else "mock LLM (set ANTHROPIC_API_KEY)"
    print(f"  LLM backend: {llm_label}")
    print(f"  Skills to evolve: {len(SKILLS)}")

    async with session_factory() as db:
        svc = SkillLifecycleService(db)
        meta_svc = MetaPromptService(db)

        # ── 1. Seed MetaPrompt archive ────────────────────────────────────
        count = await meta_svc.seed_defaults()
        print(f"\n[1] MetaPrompt archive ready ({count} mutation strategies)\n")

        # ── 2. Declare goals + seed candidates ───────────────────────────
        print("[2] Declaring goals and seeding initial skill candidates")
        release_map: dict[str, str] = {}  # skill_key → release_id

        for s in SKILLS:
            # Declare goal (human intent only)
            goal = await svc.declare_goal(
                owner="demo",
                skill_key=s["skill_key"],
                goal=s["goal"],
            )

            # Create a dummy execution to satisfy source_execution_ids
            execution = await svc.create_execution(
                owner="demo",
                sandbox_id="sb-demo",
                exec_type=ExecutionType.BROWSER,
                code=f"# demo seed for {s['skill_key']}",
                success=True,
                execution_time_ms=50,
            )

            # Create candidate
            candidate = await svc.create_candidate(
                owner="demo",
                skill_key=s["skill_key"],
                source_execution_ids=[execution.id],
                summary=s["summary"],
                usage_notes=s["usage_notes"],
                preconditions=s["preconditions"],
                postconditions=s["postconditions"],
                created_by="demo:seed",
            )

            # Evaluate and promote to canary
            await svc.evaluate_candidate(
                owner="demo", candidate_id=candidate.id, passed=True, score=0.80
            )
            release = await svc.promote_candidate(
                owner="demo", candidate_id=candidate.id, stage=SkillReleaseStage.CANARY
            )
            release_map[s["skill_key"]] = release.id

            print(f"  ✓ {s['skill_key']}")
            print(f"    goal: {s['goal'][:72]}...")
            print(f"    release: {release.id} (v{release.version}, canary)")

        # ── 3. Report failures ────────────────────────────────────────────
        print("\n[3] Reporting execution failures (evolution signals)")
        for s in SKILLS:
            release_id = release_map[s["skill_key"]]
            for reasoning in s["failures"]:
                await svc.record_outcome(
                    owner="demo",
                    skill_key=s["skill_key"],
                    release_id=release_id,
                    outcome="failure",
                    reasoning=reasoning,
                )
            print(f"  ✓ {s['skill_key']} — {len(s['failures'])} failures reported")

        # ── 4. Run evolution cycle ────────────────────────────────────────
        print("\n[4] Running LLM mutation cycle (EvolutionScheduler)")

        config = EvolutionConfig(
            enabled=True,
            min_failures_to_trigger=2,
            max_mutations_per_cycle=10,
            max_recent_outcomes=10,
        )
        llm_client = AnthropicMutationClient()
        agent = SkillMutationAgent(
            db_session=db,
            llm_client=llm_client,
            max_recent_outcomes=10,
        )
        scheduler = EvolutionScheduler(
            db_session=db,
            config=config,
            mutation_agent=agent,
        )

        result: EvolutionCycleResult = await scheduler.run_cycle()
        print(f"  Attempted: {result.mutations_attempted}")
        print(f"  Succeeded: {result.mutations_succeeded}")

        # ── 5. Show evolved candidates ────────────────────────────────────
        print("\n" + "=" * 64)
        print("  EVOLVED SKILL CANDIDATES")
        print("=" * 64)

        for s in SKILLS:
            # Find the evolution-generated candidate for this skill
            result_q = await db.execute(
                select(SkillCandidate).where(
                    SkillCandidate.owner == "demo",
                    SkillCandidate.skill_key == s["skill_key"],
                    SkillCandidate.created_by == "system:evolution",
                ).order_by(SkillCandidate.created_at.desc()).limit(1)
            )
            evolved = result_q.scalars().first()
            if evolved is None:
                print(f"\n  {s['skill_key']}: no mutation generated")
                continue

            preconditions = json.loads(evolved.preconditions_json or "[]")
            postconditions = json.loads(evolved.postconditions_json or "[]")

            print(f"\n▶ {s['skill_key']} → {evolved.id}")
            print(f"  Goal:      {s['goal']}")
            print()
            print(f"  Summary:   {evolved.summary}")
            print()
            print(f"  Usage:     {evolved.usage_notes}")
            print()
            if isinstance(preconditions, list):
                print(f"  Pre:       " + "\n             ".join(preconditions))
            if isinstance(postconditions, list):
                print(f"  Post:      " + "\n             ".join(postconditions))
            print()
            print(f"  Reasoning: {evolved.mutation_reasoning}")
            print(f"  Parent:    {evolved.evolution_parent_id}")
            print(f"  Strategy:  {evolved.evolution_meta_prompt_id}")

        print("\n" + "=" * 64)
        print(f"  {result.mutations_succeeded}/{len(SKILLS)} skills evolved")
        print("  Next: evaluate mutated candidates and promote winners to stable.")
        print("=" * 64 + "\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_demo())
