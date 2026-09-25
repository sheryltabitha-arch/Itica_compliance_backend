"""
app/routers/customer_timeline.py

GET /api/v1/customers/timeline

The "give me any customer, I'll reconstruct the chain of evidence" endpoint.
Walks customer_cases -> its decisions and kyc_documents -> their extractions
-> every audit_events row whose subject_id matches any of those ids, and
returns one chronologically ordered timeline.

ASSUMPTION (flag for review): accepts EITHER case_reference or customer_id
as the lookup key, since it wasn't confirmed which one is reliably populated
in your data yet. If a customer has more than one case, customer_id returns
all of them; case_reference returns exactly one.

COVERAGE LIMIT: audit_events.subject_id was only added in this pass (see
[[audit-hash-chain]] in project memory). Events written before that
migration have subject_id = null and will not appear here — this endpoint
reports that gap explicitly rather than silently showing a partial history
as if it were complete.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole

router = APIRouter(prefix="/api/v1/customers", tags=["customers"])


@router.get("/timeline")
async def get_customer_timeline(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    case_reference: Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
):
    if not case_reference and not customer_id:
        raise HTTPException(400, "Provide case_reference or customer_id")

    tenant_id = str(current.tenant_id)
    sb = get_supabase()

    # ── 1. Find the case(s) ──────────────────────────────────────────────
    q = sb.table("customer_cases").select("*").eq("tenant_id", tenant_id)
    q = q.eq("case_reference", case_reference) if case_reference else q.eq("customer_id", customer_id)
    cases = q.execute().data or []
    if not cases:
        raise HTTPException(404, "No case found for that reference")

    # ── 2. Customer identity (by customer_id, if we have one) ───────────
    resolved_customer_id = customer_id or cases[0].get("customer_id")
    customer = None
    if resolved_customer_id:
        cust = (
            sb.table("customers")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("customer_id", resolved_customer_id)
            .execute()
        )
        customer = cust.data[0] if cust.data else None

    decision_ids: list[str] = []
    kyc_document_ids: list[str] = []
    for c in cases:
        decision_ids += [str(x) for x in (c.get("decision_ids") or [])]
        kyc_document_ids += [str(x) for x in (c.get("kyc_document_ids") or [])]
    decision_ids = list(dict.fromkeys(decision_ids))
    kyc_document_ids = list(dict.fromkeys(kyc_document_ids))

    # ── 3. Decisions ──────────────────────────────────────────────────────
    decisions = []
    if decision_ids:
        decisions = (
            sb.table("decisions").select("*").eq("tenant_id", tenant_id)
            .in_("id", decision_ids).execute().data or []
        )

    # ── 4. KYC documents, then their extractions ────────────────────────
    kyc_documents = []
    extraction_ids: list[str] = []
    if kyc_document_ids:
        kyc_documents = (
            sb.table("kyc_documents").select("*").eq("tenant_id", tenant_id)
            .in_("id", kyc_document_ids).execute().data or []
        )
        extraction_ids = [d["extraction_id"] for d in kyc_documents if d.get("extraction_id")]

    extractions = []
    if extraction_ids:
        extractions = (
            sb.table("extractions").select("*").eq("tenant_id", tenant_id)
            .in_("id", extraction_ids).execute().data or []
        )

    # ── 5. Risk signals on the case(s) ──────────────────────────────────
    case_ids = [str(c["id"]) for c in cases]
    risk_signals = (
        sb.table("case_risk_signals").select("*").eq("tenant_id", tenant_id)
        .in_("case_id", case_ids).execute().data or []
    ) if case_ids else []

    # ── 6. Every audit event whose subject_id matches anything above ───
    subject_ids = list(dict.fromkeys(decision_ids + extraction_ids))
    audit_events = []
    if subject_ids:
        audit_events = (
            sb.table("audit_events").select("*").eq("tenant_id", tenant_id)
            .in_("subject_id", subject_ids)
            .order("created_at", desc=False)
            .execute().data or []
        )

    # ── 7. Merge into one chronological timeline ────────────────────────
    timeline = []
    for e in audit_events:
        timeline.append({
            "timestamp":  e["created_at"],
            "event_type": e["event_type"],
            "who":        e.get("created_by") or e.get("user_id"),
            "what":       e["detail"],
            "subject_id": e.get("subject_id"),
            "hash":       e.get("hash"),
        })
    for c in cases:
        if c.get("created_at"):
            timeline.append({
                "timestamp": c["created_at"], "event_type": "CASE_OPENED",
                "who": c.get("created_by"), "what": f"Case {c.get('case_reference')} opened",
                "subject_id": str(c["id"]), "hash": None,
            })
    for rs in risk_signals:
        timeline.append({
            "timestamp": rs["triggered_at"], "event_type": "RISK_SIGNAL",
            "who": None, "what": f"{rs.get('rule_code')} ({rs.get('severity')}): {rs.get('detail')}",
            "subject_id": rs.get("case_id"), "hash": None,
        })
    timeline.sort(key=lambda x: x["timestamp"] or "")

    covered_subject_ids = {e["subject_id"] for e in audit_events if e.get("subject_id")}
    missing_coverage = [sid for sid in subject_ids if sid not in covered_subject_ids]

    return {
        "resolved_customer_id": resolved_customer_id,
        "customer":             customer,
        "cases":                cases,
        "decisions":            decisions,
        "kyc_documents":        kyc_documents,
        "extractions":          extractions,
        "risk_signals":         risk_signals,
        "timeline":             timeline,
        "coverage_note": (
            "Some linked decisions/extractions have no matching audit_events "
            "row (likely written before subject_id was added) and are not "
            "represented in 'timeline' — see 'ids_missing_from_timeline'."
            if missing_coverage else
            "Every linked decision and extraction is represented in the timeline below."
        ),
        "ids_missing_from_timeline": missing_coverage,
    }
