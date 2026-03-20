"""
app/routers/reports.py
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from app.middleware.auth import CurrentUser, require_min_role
from app.models.models import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/generate")
async def generate_report(
    period_start: str,
    period_end: str,
    format: str = "pdf",
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))] = None,
):
    return {
        "status": "generated",
        "period_start": period_start,
        "period_end": period_end,
        "format": format,
        "tenant_id": current.tenant_id if current else None,
    }
