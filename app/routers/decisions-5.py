"""
app/routers/decisions.py

Changes v2.1:
  - POST /: add "created_by": current.sub to insert payload
  - POST /: add "created_by": current.sub to audit_events insert
  - GET  /: role-scoped filtering (analyst → own records; manager/admin → whole tenant)
  - GET stats unchanged
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user, get_supabase, ROLE_HIERARCHY
from app.dependencies.tenant_rules import get_tenant_rules

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/decisions", tags=["decisions"])


class DecisionCreate(BaseModel):
    decision_type:        str
    risk_tier:            str
    reference_id:         str
    rationale:            str | None = None
    officer_id:           str | None = None
    business_unit:        str | None = None
    regulatory_framework: str | None = None
    sar_required:         str | None = None
    # Lifecycle-stage tagging: links each decision to the stage of the
    # customer lifecycle it was made in, and optionally to the specific
    # regulatory obligation it satisfies (e.g. "VASP Reg 14 - EDD").
    # Required at the API level so every decision going forward is tagged;
    # NULL is only possible for rows written before this field existed.
    lifecycle_stage:      str
    obligation_ref:       str | None = None
    additional_fields:    dict | None = None


_VALID_LIFECYCLE_STAGES = {"onboarding", "ongoing_monitoring", "offboarding"}


def _missing_required_fields(payload: DecisionCreate, required_fields: list[str]) -> list[str]:
    """Checks each regulation-required field name against the payload's known
    attributes first, then falls back to the additional_fields catch-all for
    anything a regulation requires that isn't a fixed model field."""
    extra = payload.additional_fields or {}
    missing = []
    for field_name in required_fields:
        value = getattr(payload, field_name, None) if hasattr(payload, field_name) else extra.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field_name)
    return missing


# Lower-bound score for each risk tier, matching the same buckets used in
# integrations.py's _map_risk_score (Low<=39, Medium<=69, High<=89, Critical>89)
# so risk_thresholds set by regulation stay consistent across the codebase.
_TIER_FLOOR = {"low": 0, "medium": 40, "high": 70, "critical": 90}


def _tier_score(risk_tier: str) -> int:
    return _TIER_FLOOR.get((risk_tier or "").strip().lower(), 0)


def _is_truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


class DecisionResponse(BaseModel):
    id:            str
    decision_type: str
    risk_tier:     str
    reference_id:  str
    hash:          str
    created_at:    str
    tenant_id:     str


@router.post("/", response_model=DecisionResponse, status_code=status.HTTP_201_CREATED)
async def create_decision(
    payload: DecisionCreate,
    current: CurrentUser = Depends(get_current_user),
    tenant_rules: dict = Depends(get_tenant_rules),
):
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    if not tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant assigned")

    if payload.lifecycle_stage not in _VALID_LIFECYCLE_STAGES:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid lifecycle_stage",
                "allowed_values": sorted(_VALID_LIFECYCLE_STAGES),
            },
        )

    combined_required = sorted(set(tenant_rules["required_kyc_fields"]) | set(tenant_rules["mandatory_audit_fields"]))
    missing = _missing_required_fields(payload, combined_required)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Missing fields required by this tenant's applicable regulations",
                "missing_fields": missing,
                "regulations": tenant_rules["regulation_codes"],
            },
        )

    # Auto-flag SAR requirement when the tenant's merged risk_thresholds are
    # crossed, even if the caller didn't already mark sar_required themselves.
    # risk_thresholds key convention: {"sar_required_at": <tier-score cutoff>}
    sar_required_final   = payload.sar_required
    regulation_forced_sar = False
    sar_threshold = tenant_rules["risk_thresholds"].get("sar_required_at")
    if sar_threshold is not None and not _is_truthy(payload.sar_required):
        if _tier_score(payload.risk_tier) >= sar_threshold:
            sar_required_final    = "true"
            regulation_forced_sar = True

    extra_fields = dict(payload.additional_fields or {})
    if regulation_forced_sar:
        extra_fields["regulation_forced_sar"] = True
        extra_fields["regulation_forced_sar_threshold"] = sar_threshold

    hash_input = json.dumps({
        "decision_type":   payload.decision_type,
        "risk_tier":       payload.risk_tier,
        "reference_id":    payload.reference_id,
        "rationale":       payload.rationale,
        "officer_id":      payload.officer_id,
        "lifecycle_stage": payload.lifecycle_stage,
        "obligation_ref":  payload.obligation_ref,
        "tenant_id":       tenant_id,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }, sort_keys=True)
    decision_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    result = supabase.table("decisions").insert({
        "tenant_id":                tenant_id,
        "user_id":                  str(current.user_id),
        "created_by":               current.sub,          # Auth0 sub
        "decision_type":            payload.decision_type,
        "risk_tier":                payload.risk_tier,
        "reference_id":             payload.reference_id,
        "rationale":                payload.rationale,
        "officer_id":               payload.officer_id,
        "business_unit":            payload.business_unit,
        "regulatory_framework":     payload.regulatory_framework,
        "sar_required":             sar_required_final,
        "lifecycle_stage":          payload.lifecycle_stage,
        "obligation_ref":           payload.obligation_ref,
        "hash":                     decision_hash,
        "regulation_codes_applied": tenant_rules["regulation_codes"],
        "additional_fields":        extra_fields,
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

    audit_detail = (
        f"{payload.decision_type} | Risk: {payload.risk_tier} | "
        f"Ref: {payload.reference_id} | Officer: {payload.officer_id or 'N/A'} | "
        f"Stage: {payload.lifecycle_stage}"
        + (f" | Obligation: {payload.obligation_ref}" if payload.obligation_ref else "")
        + (" | SAR auto-flagged by regulation risk threshold" if regulation_forced_sar else "")
    )
    try:
        supabase.table("audit_events").insert({
            "tenant_id":     tenant_id,
            "user_id":       str(current.user_id),
            "created_by":    current.sub,                # Auth0 sub
            "event_type":    "DECISION_CREATED",
            "event_id":      f"EVT-{event_num:05d}",
            "detail":        audit_detail,
            "hash":          decision_hash,
            "previous_hash": previous_hash,
        }).execute()
    except Exception as e:
        logger.error(f"AUDIT GAP: audit_events insert failed for decision {decision['id']}, "
                     f"tenant {tenant_id}: {e!r}")
        try:
            supabase.table("audit_events_failed").insert({
                "tenant_id":   tenant_id,
                "user_id":     str(current.user_id),
                "event_type":  "DECISION_CREATED",
                "detail":      audit_detail,
                "error":       str(e),
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception:
            logger.critical(f"AUDIT GAP UNRECOVERABLE: could not log failure to fallback table "
                             f"for decision {decision['id']}, tenant {tenant_id}")

    # Default SAR filing window — 15 days is a placeholder until a signed
    # client's actual regulatory SLA for this obligation is confirmed.
    _SAR_TASK_DUE_DAYS = 15

    if _is_truthy(sar_required_final):
        try:
            supabase.table("compliance_tasks").insert({
                "tenant_id":             tenant_id,
                "title":                 f"File SAR for {payload.reference_id}",
                "description":           audit_detail,
                "assigned_officer_id":   payload.officer_id,
                "assigned_officer_name": payload.officer_id,
                "due_date":              (datetime.now(timezone.utc) + timedelta(days=_SAR_TASK_DUE_DAYS)).isoformat(),
                "priority":              "high",
                "reference_id":          payload.reference_id,
                "source":                "sar_auto",
            }).execute()
        except Exception as e:
            logger.warning(f"Auto-task creation failed for SAR decision {decision['id']} (non-fatal): {e}")

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
    limit:  int = 50,
    offset: int = 0,
    current: CurrentUser = Depends(get_current_user),
):
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    query = (
        supabase.table("decisions")
        .select("*")
        .eq("tenant_id", tenant_id)
    )

    # Analysts see only their own records; manager/admin see all tenant records
    if ROLE_HIERARCHY.get(current.role, 0) < ROLE_HIERARCHY.get("manager", 1):
        query = query.eq("created_by", current.sub)

    result = (
        query
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    count_query = (
        supabase.table("decisions")
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
    )
    if ROLE_HIERARCHY.get(current.role, 0) < ROLE_HIERARCHY.get("manager", 1):
        count_query = count_query.eq("created_by", current.sub)

    count_result = count_query.execute()

    return {
        "decisions": result.data or [],
        "total":     count_result.count or 0,
        "tenant_id": tenant_id,
    }


@router.get("/stats")
async def get_stats(current: CurrentUser = Depends(get_current_user)):
    supabase  = get_supabase()
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
        "total_decisions":       decisions_count.count    or 0,
        "total_kyc_documents":   extractions_count.count  or 0,
        "verified_kyc_documents":extractions_verified.count or 0,
        "total_audit_events":    audit_count.count        or 0,
        "tenant_id":             tenant_id,
    }
