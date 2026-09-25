"""
app/routers/cases.py

One case model, four uses:
  - case_type='kyc'                   generic customer case (existing use of customer_cases)
  - case_type='suspicious_transaction' items 16-20: alert -> analyst review -> evidence
                                        examined -> escalated -> officer decision -> filing status
  - case_type='regulatory_order'       item 21: order received -> assigned -> action taken ->
                                        records produced
  - case_type='security_incident'      item 33: detected -> assigned -> escalated -> evidence
                                        attached -> compliance decision -> notification status.
                                        Itica does not perform penetration testing or incident
                                        response — this records the COMPLIANCE response to an
                                        incident (who was told, when, what was decided), not the
                                        technical detection or remediation itself.

Rather than three tables or three routers, every case type shares the same
lifecycle machinery (status, assigned_to, an audit trail keyed on the case's
own id) and stores whatever is specific to its type in `metadata` (jsonb).
This is deliberately the smaller build: one drill-down view, one transition
endpoint, reused three ways — matching the "give me one compliance decision"
thesis rather than building three parallel case systems.

STATUS VALUES ARE NOT ENFORCED AGAINST A FIXED LIST. Different case_types
have different legitimate lifecycles (a regulatory order has no "escalated"
step; a suspicious-transaction case has no "records_produced" step). This
router does not gate which status string a case can move to — that
judgment call is left open rather than guessed at with an invented state
machine per type. If you want strict per-type transitions later, that's a
follow-up, not something to bake in blind here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole
from app.services.audit_hash import compute_event_hash

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/cases", tags=["cases"])

_VALID_CASE_TYPES = {"kyc", "suspicious_transaction", "regulatory_order", "security_incident"}


class CaseCreate(BaseModel):
    case_reference: str
    customer_name:  Optional[str] = None
    customer_id:    Optional[str] = None
    case_type:      str = "kyc"
    status:         str = "open"
    assigned_to:    Optional[str] = None
    notes:          Optional[str] = None
    metadata:       dict = {}


class CaseTransition(BaseModel):
    new_status: str
    reason:     str  # required — every transition needs a stated reason, not just a status flip
    assigned_to: Optional[str] = None
    metadata_updates: dict = {}  # merged into existing metadata, not a full replace


def _next_hash(sb, tenant_id: str) -> str:
    prev = (
        sb.table("audit_events").select("hash").eq("tenant_id", tenant_id)
        .order("created_at", desc=True).limit(1).execute()
    )
    return prev.data[0]["hash"] if prev.data else "GENESIS"


def _write_case_event(sb, tenant_id: str, current: CurrentUser, case_id: str, event_type: str, detail: str):
    previous_hash = _next_hash(sb, tenant_id)
    created_at = datetime.now(timezone.utc).isoformat()
    event_hash = compute_event_hash(tenant_id, event_type, detail, previous_hash, created_at)
    try:
        sb.table("audit_events").insert({
            "tenant_id": tenant_id, "user_id": str(current.user_id), "created_by": current.sub,
            "event_type": event_type, "detail": detail, "hash": event_hash,
            "previous_hash": previous_hash, "created_at": created_at, "subject_id": case_id,
        }).execute()
    except Exception as e:
        logger.error(f"AUDIT GAP: case event '{event_type}' failed for case {case_id}: {e!r}")


@router.post("/")
async def create_case(
    payload: CaseCreate,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    if payload.case_type not in _VALID_CASE_TYPES:
        raise HTTPException(422, f"case_type must be one of {sorted(_VALID_CASE_TYPES)}")

    tenant_id = str(current.tenant_id)
    sb = get_supabase()

    result = sb.table("customer_cases").insert({
        "tenant_id":      tenant_id,
        "case_reference": payload.case_reference,
        "customer_name":  payload.customer_name,
        "customer_id":    payload.customer_id,
        "case_type":      payload.case_type,
        "status":         payload.status,
        "assigned_to":    payload.assigned_to,
        "notes":          payload.notes,
        "metadata":       payload.metadata,
        "created_by":     current.user_id,
        "decision_ids":   [],
        "kyc_document_ids": [],
    }).execute()
    case = result.data[0]

    _write_case_event(
        sb, tenant_id, current, str(case["id"]), "CASE_OPENED",
        f"Case {payload.case_reference} opened ({payload.case_type}) | Status: {payload.status}"
        + (f" | Assigned: {payload.assigned_to}" if payload.assigned_to else ""),
    )
    return case


@router.get("/{case_reference}")
async def get_case(
    case_reference: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    """Drill-down (item 9): status, assignee, evidence count, last action —
    plus the case's own audit history, so this doubles as the suspicious-
    transaction and regulatory-order case views without a separate page."""
    tenant_id = str(current.tenant_id)
    sb = get_supabase()

    result = (
        sb.table("customer_cases").select("*")
        .eq("tenant_id", tenant_id).eq("case_reference", case_reference).execute()
    )
    if not result.data:
        raise HTTPException(404, "Case not found")
    case = result.data[0]

    evidence_count = len(case.get("decision_ids") or []) + len(case.get("kyc_document_ids") or [])

    events = (
        sb.table("audit_events").select("*")
        .eq("tenant_id", tenant_id).eq("subject_id", str(case["id"]))
        .order("created_at", desc=False).execute()
    ).data or []

    return {
        "case":            case,
        "evidence_count":  evidence_count,
        "last_action":     events[-1]["created_at"] if events else case.get("created_at"),
        "last_action_detail": events[-1]["detail"] if events else None,
        "history":         events,
    }


@router.post("/{case_reference}/transition")
async def transition_case(
    case_reference: str,
    payload: CaseTransition,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    """
    Covers every state change items 16-20 and 21 describe: alert -> analyst
    review -> evidence examined -> escalated -> officer decision -> filing
    status (suspicious transaction), and order received -> assigned ->
    action taken -> records produced (regulatory order). One endpoint, a
    required `reason` on every call, one audit event per transition.
    """
    tenant_id = str(current.tenant_id)
    sb = get_supabase()

    existing = (
        sb.table("customer_cases").select("*")
        .eq("tenant_id", tenant_id).eq("case_reference", case_reference).execute()
    )
    if not existing.data:
        raise HTTPException(404, "Case not found")
    case = existing.data[0]

    if not payload.reason.strip():
        raise HTTPException(422, "reason is required for every case transition")

    merged_metadata = {**(case.get("metadata") or {}), **payload.metadata_updates}
    update = {"status": payload.new_status, "metadata": merged_metadata, "updated_at": datetime.now(timezone.utc).isoformat()}
    if payload.assigned_to:
        update["assigned_to"] = payload.assigned_to

    sb.table("customer_cases").update(update).eq("id", case["id"]).execute()

    detail = (
        f"{case.get('status')} -> {payload.new_status} | Reason: {payload.reason} | By: {current.sub}"
        + (f" | Reassigned to: {payload.assigned_to}" if payload.assigned_to else "")
    )
    _write_case_event(sb, tenant_id, current, str(case["id"]), "CASE_TRANSITIONED", detail)

    return {
        "case_reference": case_reference,
        "previous_status": case.get("status"),
        "new_status": payload.new_status,
        "metadata": merged_metadata,
    }
