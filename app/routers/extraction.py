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

from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole
from app.inference.service import extract_document_fields, fetch_document_from_supabase

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


@router.post("/extraction")
async def extract_document(
    request: ExtractRequest,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    extraction_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Build Supabase Storage path
    storage_path = f"tenants/{current.tenant_id}/documents/{request.document_id}"

    # Fetch document from Supabase Storage
    try:
        image_bytes = fetch_document_from_supabase(storage_path)
    except Exception as e:
        logger.error(f"Supabase Storage fetch failed for {storage_path}: {e}")
        raise HTTPException(404, f"Document {request.document_id} not found in storage")

    # Run LayoutLMv3 extraction via HuggingFace Inference API
    try:
        result = extract_document_fields(image_bytes)
    except RuntimeError as e:
        if "loading" in str(e).lower():
            raise HTTPException(503, "Extraction model is warming up, please retry in 20 seconds")
        raise HTTPException(502, f"Extraction failed: {e}")

    # Store result in Supabase extractions table
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

    # Auto-flag low confidence fields
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

    # Sanctions screening
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

    # Audit log directly to Supabase
    try:
        sb = get_supabase()
        prev = sb.table("audit_events").select("hash").eq("tenant_id", str(current.tenant_id)).order("created_at", desc=True).limit(1).execute()
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
        event_count = sb.table("audit_events").select("id", count="exact").eq("tenant_id", str(current.tenant_id)).execute()
        event_num = (event_count.count or 0) + 1
        sb.table("audit_events").insert({
            "tenant_id": str(current.tenant_id),
            "user_id": str(current.user_id),
            "event_type": "EXTRACTION_COMPLETED",
            "event_id": f"EVT-{event_num:05d}",
            "detail": f"KYC extraction completed | Doc: {request.document_id} | Fields: {len(result['fields'])}",
            "hash": extraction_id,
            "previous_hash": previous_hash,
            "created_at": now.isoformat(),
        }).execute()
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
