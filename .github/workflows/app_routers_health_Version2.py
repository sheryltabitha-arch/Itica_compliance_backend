"""
Itica — Health Check Router
"""
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness():
    """Liveness probe."""
    return {"status": "alive", "version": "2.0.0"}


@router.get("/health/ready")
async def readiness():
    """Readiness probe."""
    return {"status": "ready"}