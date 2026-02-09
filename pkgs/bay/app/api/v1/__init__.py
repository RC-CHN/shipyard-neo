"""API v1 router."""

from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.capabilities import router as capabilities_router
from app.api.v1.cargos import router as cargos_router
from app.api.v1.history import router as history_router
from app.api.v1.profiles import router as profiles_router
from app.api.v1.sandboxes import router as sandboxes_router
from app.api.v1.skills import router as skills_router

router = APIRouter()

# Include sub-routers
router.include_router(sandboxes_router, prefix="/sandboxes", tags=["sandboxes"])
router.include_router(capabilities_router, prefix="/sandboxes", tags=["capabilities"])
router.include_router(history_router, prefix="/sandboxes", tags=["history"])
router.include_router(cargos_router, prefix="/cargos", tags=["cargos"])
router.include_router(skills_router, prefix="/skills", tags=["skills"])
router.include_router(profiles_router, prefix="/profiles", tags=["profiles"])
router.include_router(admin_router, prefix="/admin", tags=["admin"])
