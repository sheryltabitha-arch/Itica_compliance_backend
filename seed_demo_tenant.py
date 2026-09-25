"""
scripts/seed_demo_tenant.py

Item 36: one coherent synthetic tenant, "Acme Digital Assets Ltd", seeded
across every table this conversation has touched — customers, cases,
decisions, extractions, audit_events — so every demo screen (obligations
card, KYC extraction, decision capture, audit trail, customer timeline,
compliance dashboard) references the SAME customers and cases, rather than
each screen showing disconnected placeholder data.

Run manually, once, against a demo tenant: `python scripts/seed_demo_tenant.py`
Requires the same environment variables as the app (DATABASE_URL or
Supabase client credentials — reuses your existing app.middleware.auth
get_supabase() so it authenticates exactly as the app does).

WHY THIS RUNS PYTHON RATHER THAN RAW SQL: audit_events.hash has to be a
real chained hash computed by compute_event_hash() — the same function
extraction.py/decisions.py/cases.py use — not a random string. Seeding with
plain SQL would either fake the hash (defeating the point of the demo,
since /api/v1/audit/verify would immediately flag it as broken) or require
reimplementing the hash formula in SQL, which is more fragile than just
calling the real function.

IMPORTANT — READ BEFORE RUNNING:
  - This is NOT idempotent. Running it twice creates two of everything.
    Check for an existing "ACME-" prefixed case first if unsure:
      SELECT case_reference FROM customer_cases WHERE case_reference LIKE 'ACME-%';
  - Uses TENANT_ID below — set it to an actual demo tenant, not a real
    client's tenant_id.
  - Creates one customer with a real (invented, not a real person's)
    passport-style name and DOB. No real person's data is used.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")  # run from repo root

from app.middleware.auth import get_supabase          # noqa: E402
from app.services.audit_hash import compute_event_hash  # noqa: E402

TENANT_ID = "demo-acme"  # CHANGE to your actual demo tenant id before running


def _now(offset_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


def _write_event(sb, tenant_id, event_type, detail, subject_id, created_at, created_by="seed-script"):
    prev = (
        sb.table("audit_events").select("hash").eq("tenant_id", tenant_id)
        .order("created_at", desc=True).limit(1).execute()
    )
    previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
    event_hash = compute_event_hash(tenant_id, event_type, detail, previous_hash, created_at)
    sb.table("audit_events").insert({
        "tenant_id": tenant_id, "created_by": created_by, "event_type": event_type,
        "detail": detail, "hash": event_hash, "previous_hash": previous_hash,
        "created_at": created_at, "subject_id": subject_id,
    }).execute()
    return event_hash


def seed():
    sb = get_supabase()
    tenant_id = TENANT_ID

    # ── 1. Customer ──────────────────────────────────────────────────────
    customer_id = "CUS-ACME-001"
    sb.table("customers").insert({
        "tenant_id": tenant_id, "customer_id": customer_id,
        "full_name": "Kwame Boateng", "entity_type": "individual",
        "jurisdiction": "Ghana", "risk_rating": "medium", "risk_score": 42,
        "ubo_verified": True, "kyc_review_date": datetime.now(timezone.utc).date().isoformat(),
        "source_system": "seed_demo_tenant",
    }).execute()
    print(f"Created customer {customer_id}")

    # ── 2. Extraction (item 5/11: real screening result attached) ───────
    extraction_id = str(uuid.uuid4())
    extraction_created_at = _now(-30)
    sb.table("extractions").insert({
        "id": extraction_id, "document_id": f"DOC-{extraction_id[:8]}", "tenant_id": tenant_id,
        "model_version": "mindee-v1", "status": "completed",
        "fields": {"full_name": "Kwame Boateng", "date_of_birth": "1988-04-12", "nationality": "Ghanaian",
                   "document_number": "GHA9988771"},
        "confidence_scores": {"full_name": 0.96, "date_of_birth": 0.94, "nationality": 0.97, "document_number": 0.91},
        "overall_confidence": 0.945,
        "sanctions_result": {"screened": True, "match": False, "provider": "dilisense",
                              "screened_at": extraction_created_at, "hits": []},
        "created_at": extraction_created_at,
    }).execute()
    _write_event(sb, tenant_id, "EXTRACTION_COMPLETED",
                 "Kwame Boateng passport extracted | Overall: 94.5% | Screening: dilisense no match",
                 extraction_id, extraction_created_at)
    print(f"Created extraction {extraction_id}")

    # ── 3. KYC document row, linking the extraction ─────────────────────
    kyc_doc_id = str(uuid.uuid4())
    sb.table("kyc_documents").insert({
        "id": kyc_doc_id, "tenant_id": tenant_id, "document_id": f"DOC-{extraction_id[:8]}",
        "file_name": "passport_kboateng.pdf", "document_type": "passport",
        "origin_country": "Ghana", "hash": f"sha256-demo-{extraction_id[:16]}",
        "status": "VERIFIED", "extraction_id": extraction_id, "created_at": extraction_created_at,
    }).execute()
    print(f"Created kyc_documents row {kyc_doc_id}")

    # ── 4. Decision, approved via maker-checker (item 6) ─────────────────
    decision_created_at = _now(-20)
    dec_result = sb.table("decisions").insert({
        "tenant_id": tenant_id, "decision_type": "KYC_APPROVAL", "risk_tier": "Medium",
        "reference_id": customer_id, "rationale": "Identity verified via passport extraction; no sanctions/PEP match.",
        "officer_id": "seed-maker", "regulatory_framework": "Kenya VASP Regulations",
        "obligation_ref": "CDD", "lifecycle_stage": "onboarding", "sar_required": "false",
        "hash": f"sha256-demo-decision-{uuid.uuid4().hex[:16]}", "status": "approved",
        "approved_by": "seed-checker", "approved_at": _now(-18),
        "created_by": "seed-maker", "created_at": decision_created_at,
    }).execute()
    decision_id = str(dec_result.data[0]["id"])
    _write_event(sb, tenant_id, "DECISION_CREATED",
                 f"KYC_APPROVAL | Risk: Medium | Ref: {customer_id} | Proposed by seed-maker, approved by seed-checker",
                 decision_id, decision_created_at)
    print(f"Created decision {decision_id}")

    # ── 5. Case, linking the decision and KYC document (item 9/25) ──────
    case_reference = "ACME-CASE-001"
    case_result = sb.table("customer_cases").insert({
        "tenant_id": tenant_id, "case_reference": case_reference, "customer_name": "Kwame Boateng",
        "customer_id": customer_id, "case_type": "kyc", "status": "closed",
        "decision_ids": [decision_id], "kyc_document_ids": [kyc_doc_id],
        "notes": "Seeded demo case — coherent across obligations, KYC, decision, and audit screens.",
        "created_at": decision_created_at,
    }).execute()
    case_id = str(case_result.data[0]["id"])
    _write_event(sb, tenant_id, "CASE_OPENED", f"Case {case_reference} opened (kyc) | Status: closed",
                 case_id, decision_created_at)
    print(f"Created case {case_reference}")

    # ── 6. A second case: suspicious transaction, walked through states
    #      (items 16-20), so the case-drilldown demo has a real example ──
    st_case_reference = "ACME-CASE-002"
    st_created_at = _now(-10)
    st_result = sb.table("customer_cases").insert({
        "tenant_id": tenant_id, "case_reference": st_case_reference, "customer_name": "Kwame Boateng",
        "customer_id": customer_id, "case_type": "suspicious_transaction", "status": "escalated",
        "decision_ids": [], "kyc_document_ids": [],
        "metadata": {"alert_source": "seed_demo_tenant", "amount": "GHS 45,000"},
        "notes": "Seeded suspicious-transaction demo case.", "created_at": st_created_at,
    }).execute()
    st_case_id = str(st_result.data[0]["id"])
    for offset, status, reason in [
        (-10, "alert_received", "Aggregate volume threshold breached"),
        (-8,  "analyst_review", "Assigned to seed-analyst for review"),
        (-5,  "escalated",      "Pattern consistent with structuring — escalated to compliance officer"),
    ]:
        _write_event(sb, tenant_id, "CASE_TRANSITIONED",
                     f"-> {status} | Reason: {reason} | By: seed-script", st_case_id, _now(offset))
    print(f"Created suspicious-transaction case {st_case_reference}")

    print("\nDone. This tenant now has one coherent thread across obligations,")
    print("KYC/screening, decision capture (maker-checker), case drill-down,")
    print("and the audit trail — all traceable via:")
    print(f"  GET /api/v1/customers/timeline?customer_id={customer_id}")
    print(f"  GET /api/v1/cases/{case_reference}")
    print(f"  GET /api/v1/cases/{st_case_reference}")


if __name__ == "__main__":
    seed()
