"""Unit tests for skills payload APIs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.skills import (
    SkillPayloadCreateRequest,
    create_payload,
    get_payload,
)
from app.errors import NotFoundError, ValidationError


class _FakeSkillService:
    def __init__(self) -> None:
        self.create_kwargs: dict | None = None

    async def create_artifact_blob(self, **kwargs):
        self.create_kwargs = kwargs
        return SimpleNamespace(id="blob-1", kind=kwargs["kind"])

    def make_blob_ref(self, blob_id: str) -> str:
        return f"blob:{blob_id}"

    async def get_payload_with_blob_by_ref(self, *, owner: str, payload_ref: str):
        _ = owner
        if payload_ref == "blob:missing":
            raise NotFoundError("Artifact blob not found: missing")
        if not payload_ref.startswith("blob:"):
            raise ValidationError(f"Unsupported payload_ref: {payload_ref}")
        return (
            SimpleNamespace(id="blob-1", kind="candidate_payload"),
            {"commands": ["open about:blank"]},
        )


@pytest.mark.asyncio
async def test_create_payload_returns_blob_ref_and_kind():
    skill_svc = _FakeSkillService()

    response = await create_payload(
        request=SkillPayloadCreateRequest(
            payload={"commands": ["open about:blank"]},
            kind="candidate_payload",
        ),
        skill_svc=skill_svc,
        owner="default",
    )

    assert response.payload_ref == "blob:blob-1"
    assert response.kind == "candidate_payload"
    assert skill_svc.create_kwargs is not None
    assert skill_svc.create_kwargs["owner"] == "default"
    assert skill_svc.create_kwargs["payload"] == {"commands": ["open about:blank"]}


@pytest.mark.asyncio
async def test_get_payload_returns_payload_content_and_kind():
    skill_svc = _FakeSkillService()

    response = await get_payload(
        payload_ref="blob:blob-1",
        skill_svc=skill_svc,
        owner="default",
    )

    assert response.payload_ref == "blob:blob-1"
    assert response.kind == "candidate_payload"
    assert response.payload["commands"] == ["open about:blank"]


@pytest.mark.asyncio
async def test_get_payload_surfaces_validation_error_for_non_blob_ref():
    skill_svc = _FakeSkillService()

    with pytest.raises(ValidationError, match="Unsupported payload_ref"):
        await get_payload(
            payload_ref="s3://payload-1",
            skill_svc=skill_svc,
            owner="default",
        )


@pytest.mark.asyncio
async def test_get_payload_surfaces_not_found_for_missing_blob():
    skill_svc = _FakeSkillService()

    with pytest.raises(NotFoundError, match="Artifact blob not found"):
        await get_payload(
            payload_ref="blob:missing",
            skill_svc=skill_svc,
            owner="default",
        )
