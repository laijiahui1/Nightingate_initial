"""Central API router registry.

Every domain router (entries, patients, glance, ai-scribe, audit, auth)
registers here. ``health`` is mounted at the api_router root and re-mounted by
main.py so the container healthcheck stays on ``/healthz``.
"""

from fastapi import APIRouter

from app.api.routes.audit import router as audit_router
from app.api.routes.auth import router as auth_router
from app.api.routes.entries import router as entries_router
from app.api.routes.health import router as health_router
from app.api.routes.patients import router as patients_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(patients_router, tags=["patients"])
api_router.include_router(entries_router, tags=["entries"])
api_router.include_router(audit_router, tags=["audit"])
api_router.include_router(auth_router, tags=["auth"])
