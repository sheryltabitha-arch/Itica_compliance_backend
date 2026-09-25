from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"])

# Statuses written by extraction.py that represent work still needing a human
PENDING_STATUSES = ["requires_review", "sanctions_hit"]


class SubmitCorrectionRequest(BaseModel):
    corrections: dict[str, dict]


@router.get("/tasks")
async def list_pending_tasks(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    limit: int = 50,
    offset: int = 0,
    include_completed: bool = False,
):
    """
    Was returning EVERY extraction regardless of status — this now actually
    filters to items needing review (requires_review / sanctions_hit) unless
    include_completed=True is explicitly requested.
    """
    try:
        sb = get_supabase()
        query = (
            sb.table("extractions")
            .select("*")
            .eq("tenant_id", str(current.tenant_id))
        )
        if not include_completed:
            query = query.in_("status", PENDING_STATUSES)
        result = (
            query.order("review_priority", desc=True)  # high priority first
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
        pending = sum(1 for r in rows if r.get("status") in PENDING_STATUSES)
        all_scores = []
        for row in rows:
            scores = row.get("confidence_scores") or {}
            all_scores.extend(scores.values())
        avg_confidence = round(sum(all_scores) / len(all_scores), 4) if all_scores else 0.0
        return {
            "total_documents": total,
            "completed": completed,
            "pending": pending,
            "avg_confidence": avg_confidence,
        }
    except Exception as e:
        logger.warning(f"Stats failed: {e}")
        return {"total_documents": 0, "completed": 0, "pending": 0, "avg_confidence": 0.0}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
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
):
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

    update_result = sb.table("extractions").update({
        "fields": corrected_fields,
        "status": "reviewed",
    }).eq("id", task_id).execute()

    if not update_result.data:
        # Do not report success on a write that didn't actually happen —
        # an analyst believing a correction was saved when it wasn't is
        # worse than an error message.
        raise HTTPException(500, "Correction could not be saved — please retry")

    # Append-only audit trail entry: never edit stored history silently,
    # only append a new event recording who corrected what and when.
    tenant_id = str(current.tenant_id)
    try:
        prev = (
            sb.table("audit_events")
            .select("hash")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
        event_count = sb.table("audit_events").select("id", count="exact").eq("tenant_id", tenant_id).execute()
        event_num = (event_count.count or 0) + 1
        from app.services.audit_hash import compute_event_hash
        now_iso = datetime.now(timezone.utc).isoformat()
        correction_detail = f"Correction submitted | Task: {task_id} | Fields corrected: {len(body.corrections)}"
        correction_hash = compute_event_hash(
            tenant_id, "EXTRACTION_CORRECTED", correction_detail, previous_hash, now_iso
        )
        sb.table("audit_events").insert({
            "tenant_id":     tenant_id,
            "user_id":       str(current.user_id),
            "created_by":    current.sub,
            "event_type":    "EXTRACTION_CORRECTED",
            "event_id":      f"EVT-{event_num:05d}",
            "detail":        correction_detail,
            "hash":          correction_hash,
            "previous_hash": previous_hash,
            "created_at":    now_iso,
            "subject_id":    task_id,
        }).execute()
    except Exception as e:
        logger.error(f"AUDIT GAP: correction audit event failed for task {task_id}, tenant {tenant_id}: {e!r}")
        try:
            sb.table("audit_events_failed").insert({
                "tenant_id":   tenant_id,
                "user_id":     str(current.user_id),
                "event_type":  "EXTRACTION_CORRECTED",
                "detail":      f"Correction submitted | Task: {task_id} | Fields corrected: {len(body.corrections)}",
                "error":       str(e),
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception:
            logger.critical(f"AUDIT GAP UNRECOVERABLE: correction for task {task_id}, tenant {tenant_id}")

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
