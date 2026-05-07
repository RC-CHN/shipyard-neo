"""Rule-based extraction strategy for browser learning."""

from __future__ import annotations

import re
import shlex
from typing import Any

from app.services.skills.extraction.base import (
    ExtractionContext,
    ExtractionResult,
    VariableSpec,
)

READ_ONLY_PREFIXES = (
    "snapshot",
    "get ",
    "is ",
    "wait",
    "cookies",
    "storage",
    "network requests",
    "tab",
    "frame",
    "dialog",
)


class RuleBasedExtractionStrategy:
    """Heuristic strategy preserving historical extraction behavior."""

    def __init__(self, *, variable_extraction_enabled: bool = True) -> None:
        self._variable_extraction_enabled = variable_extraction_enabled

    async def extract(
        self,
        *,
        segments: list[list[dict[str, Any]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]:
        skill_key = self.derive_skill_key(tags=context.tags, sandbox_id=context.sandbox_id)
        description = context.description.strip() if context.description else None
        if description == "":
            description = None

        results: list[ExtractionResult] = []
        for segment in segments:
            variables = (
                self.extract_variables(segment=segment)
                if self._variable_extraction_enabled
                else None
            )
            results.append(
                ExtractionResult(
                    skill_key=skill_key,
                    description=description,
                    steps=segment,
                    variables=variables,
                )
            )
        return results

    @staticmethod
    def extract_actionable_segments(*, steps: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        segments: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []

        for step in steps:
            cmd = str(step.get("cmd", "")).strip()
            failed = int(step.get("exit_code", 1)) != 0
            is_read_only = RuleBasedExtractionStrategy.is_read_only_command(cmd)
            kind = str(step.get("kind", "individual_action"))

            if failed or is_read_only or kind != "individual_action" or not cmd:
                if len(current) >= 2:
                    segments.append(current.copy())
                current.clear()
                continue
            current.append(step)

        if len(current) >= 2:
            segments.append(current)

        return segments

    @staticmethod
    def is_read_only_command(cmd: str) -> bool:
        normalized = cmd.strip().lower()
        return any(normalized.startswith(prefix) for prefix in READ_ONLY_PREFIXES)

    @staticmethod
    def score_segment(*, segment: list[dict[str, Any]]) -> dict[str, Any]:
        steps = len(segment)
        samples = steps * 10
        replay_success = 1.0 if steps > 0 else 0.0
        score = min(0.99, 0.75 + 0.08 * steps)
        p95_duration = 0
        return {
            "score": round(score, 4),
            "replay_success": round(replay_success, 4),
            "samples": samples,
            "error_rate": round(1.0 - replay_success, 4),
            "p95_duration": p95_duration,
        }

    @staticmethod
    def derive_skill_key(*, tags: str | None, sandbox_id: str | None) -> str:
        items = [part.strip() for part in (tags or "").split(",") if part.strip()]
        for tag in items:
            if tag.startswith("skill:") and len(tag) > len("skill:"):
                return tag[len("skill:") :]
        if sandbox_id:
            return f"browser-{sandbox_id}"
        return "browser-unknown"

    @staticmethod
    def extract_variables(
        *,
        segment: list[dict[str, Any]],
    ) -> dict[str, VariableSpec] | None:
        variables: dict[str, VariableSpec] = {}
        for action_index, step in enumerate(segment):
            cmd = str(step.get("cmd", "")).strip()
            if not cmd:
                continue

            parsed = RuleBasedExtractionStrategy._parse_type_value(cmd=cmd)
            if parsed is None:
                continue

            variable_name = RuleBasedExtractionStrategy._derive_variable_name(
                value=parsed,
                existing=set(variables),
            )
            variables[variable_name] = VariableSpec(
                type="string",
                default_value=parsed,
                action_index=action_index,
                arg_position=1,
            )

        return variables or None

    @staticmethod
    def _parse_type_value(*, cmd: str) -> str | None:
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return None

        if len(tokens) < 3:
            return None
        if tokens[0].lower() != "type":
            return None

        value = " ".join(tokens[2:]).strip()
        if not value:
            return None
        return value

    @staticmethod
    def _derive_variable_name(*, value: str, existing: set[str]) -> str:
        base = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
        if not base:
            base = "value"
        if base[0].isdigit():
            base = f"value_{base}"
        base = base[:48].rstrip("_") or "value"

        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

