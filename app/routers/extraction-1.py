"""
app/routers/extraction.py
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole
from app.inference.service import extract_document_fields, fetch_document_from_s3

try:
    from app.services.audit_ledger import AuditLedger
    _audit_available = True
except ImportError:
    _audit_available = False

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["extraction"])


class ExtractRequest(BaseModel):
    document_id: str
    model_version: str = "layoutlmv3-funsd"
    country_hint: Optional[str] = None
    min_age: int = 18


class ExtractResponse(BaseModel):
    extraction_id: str
    document_id: str
    status: str
    model_version: str
    fields: dict
    confidence_scores: dict
    created_at: str


@router.post("/extraction", response_model=ExtractResponse)
async def extract_document(
    request: ExtractRequest,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    db: AsyncSession = Depends(get_db),
):
    extraction_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    s3_key = f"tenants/{current.tenant_id}/documents/{request.document_id}"

    # Fetch document from S3
    try:
        image_bytes = fetch_document_from_s3(s3_key)
    except Exception as e:
        logger.error(f"S3 fetch failed for {s3_key}: {e}")
        raise HTTPException(404, f"Document {request.document_id} not found in storage")

    # Run LayoutLMv3 extraction
    try:
        result = extract_document_fields(image_bytes)
    except RuntimeError as e:
        if "loading" in str(e).lower():
            raise HTTPException(503, "Extraction model is warming up, please retry in 20 seconds")
        raise HTTPException(502, f"Extraction failed: {e}")

    # Store result in Supabase
    try:
        sb = get_supabase()
        sb.table("extractions").insert({
            "id": extraction_id,
            "document_id": request.document_id,
            "tenant_id": str(current.tenant_id),
            "model_version": request.model_version,
            "fields": result["fields"],
            "confidence_scores": result["confidence_scores"],
            "status": "completed",
            "created_at": now.isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"Supabase store failed (non-fatal): {e}")

    # ── Auto-flag low confidence fields ──────────────────────────────────────
    LOW_CONFIDENCE_THRESHOLD = 0.75
    low_confidence_fields = {
        k: v for k, v in result["confidence_scores"].items()
        if v < LOW_CONFIDENCE_THRESHOLD
    }
    review_priority = (
        "high" if len(low_confidence_fields) >= 3
        else "medium" if low_confidence_fields
        else "low"
    )

    if low_confidence_fields:
        try:
            sb = get_supabase()
            sb.table("extractions").update({
                "status": "requires_review",
                "review_priority": review_priority,
                "low_confidence_fields": low_confidence_fields,
            }).eq("id", extraction_id).execute()
        except Exception as e:
            logger.warning(f"Review flag update failed (non-fatal): {e}")

    # ── Sanctions screening ───────────────────────────────────────────────────
    try:
        from app.services.sanctions import screen_entity
        full_name   = result["fields"].get("full_name", "")
        dob         = result["fields"].get("date_of_birth", "")
        nationality = result["fields"].get("nationality", "")
        if full_name:
            sanctions_result = screen_entity(full_name, dob, nationality)
            if sanctions_result.get("match"):
                sb = get_supabase()
                sb.table("extractions").update({
                    "status": "sanctions_hit",
                    "review_priority": "high",
                    "sanctions_result": sanctions_result,
                }).eq("id", extraction_id).execute()
                logger.warning(f"SANCTIONS HIT: {full_name} | extraction {extraction_id}")
    except Exception as e:
        logger.warning(f"Sanctions screening failed (non-fatal): {e}")

    # ── Audit log ─────────────────────────────────────────────────────────────
    if _audit_available:
        try:
            audit = AuditLedger(db)
            await audit.record(
                tenant_id=current.tenant_id,
                action_type="extraction_completed",
                user_id=current.user_id,
                resource_type="extraction",
                resource_id=extraction_id,
                payload={
                    "document_id": request.document_id,
                    "field_count": len(result["fields"]),
                },
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"Audit record failed (non-fatal): {e}")

    return ExtractResponse(
        extraction_id=extraction_id,
        document_id=request.document_id,
        status="completed",
        model_version=request.model_version,
        fields=result["fields"],
        confidence_scores=result["confidence_scores"],
        created_at=now.isoformat(),
    )


@router.get("/extraction/{extraction_id}")
async def get_extraction_result(
    extraction_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    db: AsyncSession = Depends(get_db),
):
    try:
        sb = get_supabase()
        result = (
            sb.table("extractions")
            .select("*")
            .eq("id", extraction_id)
            .eq("tenant_id", str(current.tenant_id))
            .execute()
        )
        if not result.data:
            raise HTTPException(404, f"Extraction {extraction_id} not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Extraction lookup failed: {e}")
        raise HTTPException(500, "Could not retrieve extraction result")
