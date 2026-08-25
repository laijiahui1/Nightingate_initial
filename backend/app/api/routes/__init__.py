"""Central API router registry.

Every domain router (entries, patients, glance, ai-scribe, audit, auth,
comments, tasks, mentions, users) registers here. ``health`` is mounted at the
api_router root and re-mounted by main.py so the container healthcheck stays on
``/healthz``.
"""

from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.audit import router as audit_router
from app.api.routes.auth import router as auth_router
from app.api.routes.comments import router as comments_router
from app.api.routes.entries import router as entries_router
from app.api.routes.health import router as health_router
from app.api.routes.highlights import router as highlights_router
from app.api.routes.mentions import router as mentions_router
from app.api.routes.patients import router as patients_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.users import router as users_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(admin_router, tags=["admin"])
api_router.include_router(patients_router, tags=["patients"])
api_router.include_router(entries_router, tags=["entries"])
api_router.include_router(audit_router, tags=["audit"])
api_router.include_router(auth_router, tags=["auth"])
api_router.include_router(comments_router, tags=["comments"])
api_router.include_router(tasks_router, tags=["tasks"])
api_router.include_router(mentions_router, tags=["mentions"])
api_router.include_router(users_router, tags=["users"])
api_router.include_router(highlights_router, tags=["highlights"])
