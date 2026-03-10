from __future__ import annotations
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.middleware.auth import CurrentUser, require_min_role
from app.models.models import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"])

class SubmitCorrectionRequest(BaseModel):
    corrections: dict[str, dict]

@router.get("/tasks")
async def list_pending_tasks(current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
                             db: AsyncSession = Depends(get_db), limit: int = 50, offset: int = 0):
    return {"tasks": [], "count": 0}

@router.get("/tasks/{task_id}")
async def get_task(task_id: str,
                   current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
                   db: AsyncSession = Depends(get_db)):
    raise HTTPException(404, f"Task {task_id} not found")

@router.post("/tasks/{task_id}/correct")
async def submit_correction(task_id: str, body: SubmitCorrectionRequest, request: Request,
                            current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer)),
                            db: AsyncSession = Depends(get_db)):
    return {"correction_id": f"corr-{task_id}", "status": "submitted",
            "task_id": task_id, "fields_corrected": len(body.corrections)}

@router.get("/reason-codes")
async def list_reason_codes():
    return {"reason_codes": ["extraction_error", "low_confidence", "document_quality",
                             "format_mismatch", "manual_verification", "fraud_detected", "other"]}
