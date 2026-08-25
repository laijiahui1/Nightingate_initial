"""Central API router registry.

Every domain router (entries, sections, comments, highlights, revisions,
tasks, patients, glance, ai-scribe) registers here as it lands in M1+. New
routers must be mounted with their path prefix and tag, e.g.:

    from app.api.routes.patients import router as patients_router
    api_router.include_router(patients_router, prefix="/patients", tags=["patients"])
"""

from fastapi import APIRouter

from app.api.routes.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router)
