"""
app/routers/decisions.py
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user, get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/decisions", tags=["decisions"])


class DecisionCreate(BaseModel):
    decision_type: str
    risk_tier: str
    reference_id: str
    rationale: str | None = None
    officer_id: str | None = None
    business_unit: str | None = None
    regulatory_framework: str | None = None
    sar_required: str | None = None


class DecisionResponse(BaseModel):
    id: str
    decision_type: str
    risk_tier: str
    reference_id: str
    hash: str
    created_at: str
    tenant_id: str


@router.post("/", response_model=DecisionResponse, status_code=status.HTTP_201_CREATED)
async def create_decision(
    payload: DecisionCreate,
    current: CurrentUser = Depends(get_current_user),
):
    supabase = get_supabase()
    tenant_id = str(current.tenant_id)

    if not tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant assigned")

    hash_input = json.dumps({
        "decision_type": payload.decision_type,
        "risk_tier": payload.risk_tier,
        "reference_id": payload.reference_id,
        "rationale": payload.rationale,
        "officer_id": payload.officer_id,
        "tenant_id": tenant_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, sort_keys=True)
    decision_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    result = supabase.table("decisions").insert({
        "tenant_id": tenant_id,
        "user_id": str(current.user_id),
        "decision_type": payload.decision_type,
        "risk_tier": payload.risk_tier,
        "reference_id": payload.reference_id,
        "rationale": payload.rationale,
        "officer_id": payload.officer_id,
        "business_unit": payload.business_unit,
        "regulatory_framework": payload.regulatory_framework,
        "sar_required": payload.sar_required,
        "hash": decision_hash,
    }).execute()
    decision = result.data[0]

    prev = (
        supabase.table("audit_events")
        .select("hash")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"

    event_count = (
        supabase.table("audit_events")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    event_num = (event_count.count or 0) + 1

    supabase.table("audit_events").insert({
        "tenant_id": tenant_id,
        "user_id": str(current.user_id),
        "event_type": "DECISION_CREATED",
        "event_id": f"EVT-{event_num:05d}",
        "detail": (
            f"{payload.decision_type} | Risk: {payload.risk_tier} | "
            f"Ref: {payload.reference_id} | Officer: {payload.officer_id or 'N/A'}"
        ),
        "hash": decision_hash,
        "previous_hash": previous_hash,
    }).execute()

    return DecisionResponse(
        id=decision["id"],
        decision_type=decision["decision_type"],
        risk_tier=decision["risk_tier"],
        reference_id=decision["reference_id"],
        hash=decision_hash,
        created_at=decision["created_at"],
        tenant_id=tenant_id,
    )


@router.get("/")
async def list_decisions(
    limit: int = 50,
    offset: int = 0,
    current: CurrentUser = Depends(get_current_user),
):
    supabase = get_supabase()
    tenant_id = str(current.tenant_id)

    result = (
        supabase.table("decisions")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    count_result = (
        supabase.table("decisions")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    return {
        "decisions": result.data or [],
        "total": count_result.count or 0,
        "tenant_id": tenant_id,
    }


@router.get("/stats")
async def get_stats(current: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    tenant_id = str(current.tenant_id)

    decisions_count = (
        supabase.table("decisions")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    extractions_count = (
        supabase.table("extractions")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    extractions_verified = (
        supabase.table("extractions")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .eq("status", "reviewed")
        .execute()
    )
    audit_count = (
        supabase.table("audit_events")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
        .execute()
    )

    return {
        "total_decisions": decisions_count.count or 0,
        "total_kyc_documents": extractions_count.count or 0,
        "verified_kyc_documents": extractions_verified.count or 0,
        "total_audit_events": audit_count.count or 0,
        "tenant_id": tenant_id,
    }
