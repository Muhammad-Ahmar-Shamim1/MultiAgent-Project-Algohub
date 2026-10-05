"""Liveness check."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Used by Docker healthchecks and smoke tests."""
    return {"status": "ok"}
