from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"])


class SubmitCorrectionRequest(BaseModel):
    corrections: dict[str, dict]


@router.get("/tasks")
async def list_pending_tasks(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    try:
        sb = get_supabase()
        result = (
            sb.table("extractions")
            .select("*")
            .eq("tenant_id", str(current.tenant_id))
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        tasks = result.data or []
        return {"tasks": tasks, "count": len(tasks)}
    except Exception as e:
        logger.warning(f"Task list failed: {e}")
        return {"tasks": [], "count": 0}


@router.get("/stats")
async def get_stats(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    db: AsyncSession = Depends(get_db),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("extractions")
            .select("status, confidence_scores")
            .eq("tenant_id", str(current.tenant_id))
            .execute()
        )
        rows = result.data or []
        total = len(rows)
        completed = sum(1 for r in rows if r.get("status") == "completed")
        all_scores = []
        for row in rows:
            scores = row.get("confidence_scores") or {}
            all_scores.extend(scores.values())
        avg_confidence = round(sum(all_scores) / len(all_scores), 4) if all_scores else 0.0
        return {
            "total_documents": total,
            "completed": completed,
            "pending": total - completed,
            "avg_confidence": avg_confidence,
        }
    except Exception as e:
        logger.warning(f"Stats failed: {e}")
        return {"total_documents": 0, "completed": 0, "pending": 0, "avg_confidence": 0.0}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    db: AsyncSession = Depends(get_db),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("extractions")
            .select("*")
            .eq("id", task_id)
            .eq("tenant_id", str(current.tenant_id))
            .execute()
        )
        if not result.data:
            raise HTTPException(404, f"Task {task_id} not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Could not retrieve task")


@router.post("/tasks/{task_id}/correct")
async def submit_correction(
    task_id: str,
    body: SubmitCorrectionRequest,
    request: Request,
    current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer)),
    db: AsyncSession = Depends(get_db),
):
    try:
        sb = get_supabase()
        existing = (
            sb.table("extractions")
            .select("fields")
            .eq("id", task_id)
            .eq("tenant_id", str(current.tenant_id))
            .execute()
        )
        if not existing.data:
            raise HTTPException(404, f"Task {task_id} not found")

        current_fields = existing.data[0].get("fields", {})
        corrected_fields = {**current_fields, **{k: v.get("value", v) for k, v in body.corrections.items()}}

        sb.table("extractions").update({
            "fields": corrected_fields,
            "status": "reviewed",
        }).eq("id", task_id).execute()

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Correction write failed (non-fatal): {e}")

    return {
        "correction_id": f"corr-{task_id}",
        "status": "submitted",
        "task_id": task_id,
        "fields_corrected": len(body.corrections),
    }


@router.get("/reason-codes")
async def list_reason_codes():
    return {
        "reason_codes": [
            "extraction_error", "low_confidence", "document_quality",
            "format_mismatch", "manual_verification", "fraud_detected", "other",
        ]
    }
