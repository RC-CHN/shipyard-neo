"""Profiles API endpoints.

GET /v1/profiles - List available profiles
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import ProfileConfig, get_settings

router = APIRouter()


class ResourceSpecResponse(BaseModel):
    """Resource specification response."""

    cpus: float
    memory: str


class ProfileResponse(BaseModel):
    """Profile response model."""

    id: str
    image: str
    resources: ResourceSpecResponse
    capabilities: list[str]
    idle_timeout: int


class ProfileListResponse(BaseModel):
    """Profile list response."""

    items: list[ProfileResponse]


def _profile_to_response(p: ProfileConfig) -> ProfileResponse:
    """Convert ProfileConfig to API response, handling multi-container profiles."""
    primary = p.get_primary_container()

    # Image: use legacy field or primary container image
    image = p.image or (primary.image if primary else "unknown")

    # Resources: use legacy field or primary container resources
    if p.resources is not None:
        resources = ResourceSpecResponse(cpus=p.resources.cpus, memory=p.resources.memory)
    elif primary is not None:
        resources = ResourceSpecResponse(cpus=primary.resources.cpus, memory=primary.resources.memory)
    else:
        resources = ResourceSpecResponse(cpus=1.0, memory="1g")

    # Capabilities: use legacy field or aggregate from all containers
    if p.capabilities is not None:
        capabilities = p.capabilities
    else:
        capabilities = sorted(p.get_all_capabilities())

    return ProfileResponse(
        id=p.id,
        image=image,
        resources=resources,
        capabilities=capabilities,
        idle_timeout=p.idle_timeout,
    )


@router.get("", response_model=ProfileListResponse)
async def list_profiles() -> ProfileListResponse:
    """List available profiles."""
    settings = get_settings()
    items = [_profile_to_response(p) for p in settings.profiles]
    return ProfileListResponse(items=items)
