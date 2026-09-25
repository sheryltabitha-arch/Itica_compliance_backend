"""
app/routers/extraction.py
"""
from __future__ import annotations

import hashlib
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
    document_id:   str
    model_version: str           = "impira/layoutlm-document-qa"
    country_hint:  Optional[str] = None
    min_age:       int           = 18


class ExtractResponse(BaseModel):
    extraction_id:      str
    document_id:        str
    status:             str
    model_version:      str
    fields:             dict
    confidence_scores:  dict
    overall_confidence: float | None
    created_at:         str
    screening:          dict | None = None


@router.post("/extraction")
async def extract_document(
    request: ExtractRequest,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    extraction_id = str(uuid.uuid4())
    now           = datetime.now(timezone.utc)
    storage_path  = f"tenants/{current.tenant_id}/documents/{request.document_id}"

    # ── Fetch document from Supabase Storage ─────────────────────────────────
    try:
        image_bytes = fetch_document_from_supabase(storage_path)
    except Exception as e:
        logger.error(f"Supabase Storage fetch failed for {storage_path}: {e}")
        raise HTTPException(404, f"Document {request.document_id} not found in storage")

    # ── Run extraction ────────────────────────────────────────────────────────
    try:
        result = extract_document_fields(
            image_bytes,
            storage_path=storage_path,
            document_type=request.model_version or "passport",
        )
    except RuntimeError as e:
        if "loading" in str(e).lower():
            raise HTTPException(503, "Extraction model is warming up, please retry in 20 seconds")
        raise HTTPException(502, f"Extraction failed: {e}")

    # service.py already computes low_confidence_fields — no need to recalculate
    low_confidence_fields = result["low_confidence_fields"]
    overall_confidence    = result["overall_confidence"]
    review_priority = (
        "high"   if len(low_confidence_fields) >= 3
        else "medium" if low_confidence_fields
        else "low"
    )
    extraction_status = "requires_review" if low_confidence_fields else "completed"

    # ── Store result in Supabase ──────────────────────────────────────────────
    sb = get_supabase()
    try:
        sb.table("extractions").insert({
            "id":                    extraction_id,
            "document_id":           request.document_id,
            "tenant_id":             str(current.tenant_id),
            "model_version":         request.model_version,
            "fields":                result["fields"],
            "confidence_scores":     result["confidence_scores"],
            "overall_confidence":    overall_confidence,
            "low_confidence_fields": low_confidence_fields,
            "status":                extraction_status,
            "review_priority":       review_priority,
            "created_at":            now.isoformat(),
        }).execute()
    except Exception as e:
        # This is the primary deliverable, not a secondary audit record —
        # there's no reasonable fallback if it can't be stored. Returning
        # "success" here would mean the caller gets a full ExtractResponse
        # for data that was never persisted, and a later GET on this same
        # extraction_id would 404 despite the earlier "success" response.
        logger.error(f"Failed to store extraction {extraction_id} for document "
                     f"{request.document_id}, tenant {current.tenant_id}: {e!r}")
        raise HTTPException(
            status_code=502,
            detail="Extraction was computed but could not be saved — please retry",
        )

    # ── Sanctions screening ───────────────────────────────────────────────────
    screening_out = {"screened": False, "detail": "No name extracted - screening not run"}
    try:
        from app.services.sanctions import screen_entity
        full_name   = result["fields"].get("full_name", "")
        dob         = result["fields"].get("date_of_birth", "")
        nationality = result["fields"].get("nationality", "")
        if full_name:
            sanctions_result = screen_entity(full_name, dob, nationality)
            screening_out = sanctions_result
            if sanctions_result.get("match"):
                sb = get_supabase()
                sb.table("extractions").update({
                    "status":           "sanctions_hit",
                    "review_priority":  "high",
                    "sanctions_result": sanctions_result,
                }).eq("id", extraction_id).execute()
                logger.warning(f"SANCTIONS HIT: {full_name} | extraction {extraction_id}")
                extraction_status = "sanctions_hit"
            elif not sanctions_result.get("screened", False):
                # screen_entity() can return a well-formed "no match" result
                # even when nothing was actually checked — e.g. when
                # SANCTIONS_API_URL isn't configured, it returns
                # {"screened": False, "match": False, ...} rather than
                # raising. That must NOT be treated the same as a genuine
                # clear result, or every unconfigured screening silently
                # looks identical to a passed one.
                sb = get_supabase()
                sb.table("extractions").update({
                    "status":           "requires_review",
                    "review_priority":  "high",
                    "sanctions_result": sanctions_result,
                }).eq("id", extraction_id).execute()
                logger.warning(
                    f"SANCTIONS SCREENING NOT PERFORMED (forcing review): "
                    f"extraction {extraction_id} | detail: {sanctions_result.get('detail')}"
                )
                extraction_status = "requires_review"
            else:
                # Screening ran and returned no match: keep the evidence
                # (provider + timestamp) on the extraction record.
                sb = get_supabase()
                sb.table("extractions").update({
                    "sanctions_result": sanctions_result,
                }).eq("id", extraction_id).execute()
    except Exception as e:
        # Screening failing must NEVER be treated as screening passing.
        # A document that couldn't be checked against sanctions lists is
        # not "completed" — it needs a human to confirm screening
        # separately, and that must be visible, not silent.
        logger.error(f"SANCTIONS SCREENING FAILED (forcing review): extraction {extraction_id}: {e!r}")
        screening_out = {"screened": False, "detail": "Screening failed - screen manually before approving"}
        try:
            sb = get_supabase()
            sb.table("extractions").update({
                "status":           "requires_review",
                "review_priority":  "high",
                "sanctions_result": {"error": "screening_failed", "detail": str(e)},
            }).eq("id", extraction_id).execute()
            extraction_status = "requires_review"
        except Exception as inner_e:
            logger.critical(
                f"Could not flag extraction {extraction_id} for review after "
                f"sanctions screening failure: {inner_e!r}"
            )

    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        sb          = get_supabase()
        prev        = sb.table("audit_events").select("hash").eq("tenant_id", str(current.tenant_id)).order("created_at", desc=True).limit(1).execute()
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
        event_count = sb.table("audit_events").select("id", count="exact").eq("tenant_id", str(current.tenant_id)).execute()
        event_num   = (event_count.count or 0) + 1
        audit_detail = (
            f"KYC extraction completed | Doc: {request.document_id} | "
            f"Fields: {len(result['fields'])} | "
            f"Overall confidence: {round(overall_confidence * 100)}% | "
            f"Status: {extraction_status}"
            if overall_confidence is not None
            else
            f"KYC extraction completed | Doc: {request.document_id} | "
            f"Fields: {len(result['fields'])} | Status: {extraction_status}"
        )
        if not screening_out.get("screened", False):
            audit_detail += " | Screening: NOT PERFORMED"
        elif screening_out.get("match"):
            audit_detail += (f" | Screening: {screening_out.get('provider')} POTENTIAL MATCH "
                             f"({len(screening_out.get('hits') or [])} hit(s))")
        else:
            audit_detail += f" | Screening: {screening_out.get('provider')} no match"
        # Shared with decisions.py / human_review.py / integrations.py, so a
        # verifier only needs one formula. Previously this field held
        # extraction_id, which isn't derived from previous_hash and can't be
        # used to verify the chain.
        from app.services.audit_hash import compute_event_hash
        event_hash = compute_event_hash(
            str(current.tenant_id), "EXTRACTION_COMPLETED", audit_detail, previous_hash, now.isoformat()
        )
        sb.table("audit_events").insert({
            "tenant_id":     str(current.tenant_id),
            "user_id":       str(current.user_id),
            "created_by":    current.sub,
            "event_type":    "EXTRACTION_COMPLETED",
            "event_id":      f"EVT-{event_num:05d}",
            "detail":        audit_detail,
            "hash":          event_hash,
            "previous_hash": previous_hash,
            "created_at":    now.isoformat(),
            "subject_id":    extraction_id,
        }).execute()
    except Exception as e:
        logger.error(f"AUDIT GAP: audit_events insert failed for extraction {extraction_id}, "
                     f"tenant {current.tenant_id}: {e!r}")
        try:
            sb.table("audit_events_failed").insert({
                "tenant_id":   str(current.tenant_id),
                "user_id":     str(current.user_id),
                "event_type":  "EXTRACTION_COMPLETED",
                "detail":      audit_detail,
                "error":       str(e),
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception:
            logger.critical(f"AUDIT GAP UNRECOVERABLE: could not log failure to fallback table "
                             f"for extraction {extraction_id}, tenant {current.tenant_id}")

    return ExtractResponse(
        extraction_id=     extraction_id,
        document_id=       request.document_id,
        status=            extraction_status,
        model_version=     request.model_version,
        fields=            result["fields"],
        confidence_scores= result["confidence_scores"],
        overall_confidence=overall_confidence,
        created_at=        now.isoformat(),
        screening=         screening_out,
    )


@router.get("/extraction/{extraction_id}")
async def get_extraction_result(
    extraction_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    try:
        sb     = get_supabase()
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


# ── Screening disposition ────────────────────────────────────────────────
# The screening card's UI copy ("Potential match — reviewer confirmation
# required") implied this existed since it was first built; it didn't.
# This is that missing action: a reviewer dismisses a potential match with
# a stated reason, or escalates it into a suspicious-transaction case.
class ScreeningDisposition(BaseModel):
    action: str  # "dismiss" | "escalate"
    reason: str


@router.post("/extraction/{extraction_id}/screening-disposition")
async def disposition_screening_hit(
    extraction_id: str,
    payload: ScreeningDisposition,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    if payload.action not in ("dismiss", "escalate"):
        raise HTTPException(422, "action must be 'dismiss' or 'escalate'")
    if not payload.reason.strip():
        raise HTTPException(422, "reason is required")

    tenant_id = str(current.tenant_id)
    sb = get_supabase()

    result = (
        sb.table("extractions").select("*")
        .eq("id", extraction_id).eq("tenant_id", tenant_id).execute()
    )
    if not result.data:
        raise HTTPException(404, f"Extraction {extraction_id} not found")
    extraction = result.data[0]

    sanctions_result = extraction.get("sanctions_result") or {}
    if not sanctions_result.get("match"):
        raise HTTPException(409, "This extraction has no unresolved screening match to act on")

    now_iso = datetime.now(timezone.utc).isoformat()
    sanctions_result["disposition"] = {
        "action": payload.action, "reason": payload.reason,
        "by": current.sub, "at": now_iso,
    }
    sb.table("extractions").update({
        "sanctions_result": sanctions_result,
        # Dismissing clears the review block this hit forced; escalating
        # keeps it in review — a case now exists to track it, but the
        # extraction itself isn't cleared for approval on its own say-so.
        "status": "completed" if payload.action == "dismiss" else "requires_review",
    }).eq("id", extraction_id).execute()

    from app.services.audit_hash import compute_event_hash
    prev = (
        sb.table("audit_events").select("hash").eq("tenant_id", tenant_id)
        .order("created_at", desc=True).limit(1).execute()
    )
    previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
    detail = (
        f"Screening match {payload.action}d | Extraction: {extraction_id} | "
        f"Reason: {payload.reason} | By: {current.sub}"
    )
    event_hash = compute_event_hash(tenant_id, "SCREENING_DISPOSITIONED", detail, previous_hash, now_iso)
    try:
        sb.table("audit_events").insert({
            "tenant_id": tenant_id, "user_id": str(current.user_id), "created_by": current.sub,
            "event_type": "SCREENING_DISPOSITIONED", "detail": detail, "hash": event_hash,
            "previous_hash": previous_hash, "created_at": now_iso, "subject_id": extraction_id,
        }).execute()
    except Exception as e:
        logger.error(f"AUDIT GAP: screening disposition event failed for extraction {extraction_id}: {e!r}")

    case_reference = None
    if payload.action == "escalate":
        # Same case model built for items 16-20/33/50 — an escalated hit
        # becomes a real suspicious-transaction case, not just a flag that
        # sits on the extraction with nowhere to go.
        case_reference = f"CASE-SCREEN-{extraction_id[:8]}"
        sb.table("customer_cases").insert({
            "tenant_id": tenant_id, "case_reference": case_reference,
            "case_type": "suspicious_transaction", "status": "escalated",
            "decision_ids": [], "kyc_document_ids": [],
            "metadata": {"source": "screening_escalation", "extraction_id": extraction_id,
                         "sanctions_hits": sanctions_result.get("hits", [])},
            "notes": f"Auto-opened from screening escalation | Reason: {payload.reason}",
            "created_by": current.user_id,
        }).execute()
        try:
            sb.table("audit_events").insert({
                "tenant_id": tenant_id, "user_id": str(current.user_id), "created_by": current.sub,
                "event_type": "CASE_OPENED",
                "detail": f"Case {case_reference} opened (suspicious_transaction) from screening escalation",
                "hash": compute_event_hash(tenant_id, "CASE_OPENED", case_reference, event_hash, now_iso),
                "previous_hash": event_hash, "created_at": now_iso, "subject_id": case_reference,
            }).execute()
        except Exception as e:
            logger.error(f"AUDIT GAP: case-open event failed for {case_reference}: {e!r}")

    return {
        "extraction_id": extraction_id, "action": payload.action, "reason": payload.reason,
        "case_reference": case_reference,
        "message": (
            "Match dismissed — extraction cleared for the normal approval flow."
            if payload.action == "dismiss" else
            f"Escalated — case {case_reference} opened for investigation."
        ),
    }
