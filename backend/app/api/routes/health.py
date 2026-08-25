"""Health probe: liveness + database reachability."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter()


@router.get("/healthz", tags=["health"])
def healthz(db: Session = Depends(get_db)) -> dict:
    """Return ok only when the API can execute a query against Postgres.

    This is the container healthcheck target; `make up` blocks on it before
    declaring the stack healthy.
    """
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
