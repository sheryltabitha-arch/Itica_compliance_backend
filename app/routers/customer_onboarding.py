"""
app/routers/customer_onboarding.py

Customer onboarding from a KYC extraction, per-customer transaction view,
and a transparent risk score driven by sanctions/PEP screening.

Flow
  1. User extracts a KYC document (POST /api/v1/extraction) -> sanctions/PEP
     screening already runs there and is stored on extractions.sanctions_result.
  2. GET  /api/v1/customer-profiles/onboarding-preview/{extraction_id}
        server derives the profile fields + risk preview from the STORED
        extraction (never from client-supplied screening results).
  3. POST /api/v1/customer-profiles/onboard
        creates the row in `customers`, scores risk, writes a hash-chained
        audit event.
  4. GET  /api/v1/customer-profiles/{customer_id}
        profile + every transaction linked to that customer_id.
  5. POST /api/v1/customer-profiles/{customer_id}/rescore
        re-runs scoring using the stored screening + current transactions.

Design rules (copied from extraction.py / cases.py so behaviour is consistent)
  * Screening that did not run is NEVER treated as clear.
  * An unresolved sanctions/PEP match blocks onboarding until a reviewer has
    dismissed or escalated it (POST /extraction/{id}/screening-disposition).
  * Everything is tenant-scoped via current.tenant_id.
  * Python 3.9 compatible (uses Optional[], not `X | None`, in pydantic models).

Prefix is /api/v1/customer-profiles on purpose: /api/v1/customers/timeline is
already taken by customer_timeline.py and a /{customer_id} route there would
shadow it depending on registration order.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_supabase, require_min_role
from app.models.models import UserRole
from app.services.audit_hash import compute_event_hash

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/customer-profiles", tags=["customer-profiles"])


# ── Scoring configuration ────────────────────────────────────────────────────
# ⚠ POLICY PLACEHOLDERS. These weights are an explainable starting point, NOT a
# regulator-approved risk methodology. Your compliance lead must review/sign
# them off (and document the methodology) before they drive real decisions.
WEIGHTS = {
    "sanctions_escalated": 90,   # unresolved/escalated sanctions-type hit
    "pep_match":           40,
    "high_risk_jurisdiction":  35,
    "elevated_jurisdiction":   15,
    "corporate_ubo_unverified": 25,
    "low_extraction_confidence": 10,
    "tx_flagged_each":      5,    # per flagged transaction ...
    "tx_flagged_cap":      20,    # ... capped
    "tx_high_score":       10,    # any transaction with risk_score >= 80
    "network_strong_open": 15,    # wallet shared with other customers, not yet reviewed
    "network_escalated":   25,    # shared-counterparty link escalated by a reviewer
}
LOW_CONF_THRESHOLD = 0.75
# Low <40, Medium <70, High >=70 — matches integrations._map_risk_score
# (Low<=39, Medium<=69, High>=70) and collapses its "Critical" into "high" so we
# never write a value a customers.risk_rating CHECK constraint might reject.
MEDIUM_FROM, HIGH_FROM = 40, 70

# Next KYC review interval by rating (days). Policy placeholder — confirm with
# your regulator's requirements.
REVIEW_INTERVAL_DAYS = {"low": 730, "medium": 365, "high": 180}


def _csv_env(name: str) -> set:
    return {x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()}


# Jurisdiction risk lists are deliberately NOT hardcoded: FATF/EU/national lists
# change several times a year. Set these on the server as comma-separated
# country names, e.g. HIGH_RISK_JURISDICTIONS="North Korea,Iran,Myanmar".
if not os.environ.get("HIGH_RISK_JURISDICTIONS"):
    logger.warning(
        "HIGH_RISK_JURISDICTIONS not set — jurisdiction risk will not add to any "
        "customer's score until it is configured."
    )


# ── Pure helpers (unit-tested in tests/test_customer_onboarding.py) ──────────

def classify_screening(sanctions_result: Optional[dict]) -> Dict[str, Any]:
    """
    Reduce a stored extractions.sanctions_result to what scoring needs.

    state:
      unscreened      screening did not run / failed / missing  -> must NOT onboard
      clear           ran, no hits
      needs_review    hits exist, no disposition yet            -> must NOT onboard
      dismissed       hits exist, reviewer dismissed as false positive
      escalated       hits exist, reviewer escalated
    """
    sr = sanctions_result or {}
    if sr.get("error") or not sr.get("screened", False):
        return {"state": "unscreened", "pep": False, "sanctions": False, "hits": []}

    hits = sr.get("hits") or []
    if not sr.get("match") and not hits:
        return {"state": "clear", "pep": False, "sanctions": False, "hits": []}

    def _is_pep(h: dict) -> bool:
        return bool(h.get("pep_type")) or "pep" in str(h.get("source_type") or "").lower()

    pep = any(_is_pep(h) for h in hits)
    sanc = any(not _is_pep(h) for h in hits)
    disp = (sr.get("disposition") or {}).get("action")
    state = "dismissed" if disp == "dismiss" else "escalated" if disp == "escalate" else "needs_review"
    return {"state": state, "pep": pep, "sanctions": sanc, "hits": hits}


def band(score: int) -> str:
    return "high" if score >= HIGH_FROM else "medium" if score >= MEDIUM_FROM else "low"


def score_customer(
    *,
    entity_type: str,
    jurisdiction: str,
    ubo_verified: Optional[bool],
    screening: Dict[str, Any],
    overall_confidence: Optional[float] = None,
    tx_summary: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, List[Dict[str, Any]]]:
    """Returns (score 0-100, rating low|medium|high, factors[]). Fully explainable."""
    factors: List[Dict[str, Any]] = []

    def add(code: str, label: str, pts: int):
        factors.append({"code": code, "label": label, "points": pts})

    state = screening.get("state")
    if state == "escalated" and screening.get("sanctions"):
        add("sanctions_escalated", "Escalated sanctions-list match", WEIGHTS["sanctions_escalated"])
    if state in ("escalated", "needs_review") and screening.get("pep"):
        add("pep_match", "Politically exposed person (PEP) match — EDD required", WEIGHTS["pep_match"])
    # A dismissed hit (reviewer confirmed false positive) adds nothing, by design.

    j = (jurisdiction or "").strip().lower()
    if j and j in _csv_env("HIGH_RISK_JURISDICTIONS"):
        add("high_risk_jurisdiction", f"High-risk jurisdiction: {jurisdiction}", WEIGHTS["high_risk_jurisdiction"])
    elif j and j in _csv_env("ELEVATED_JURISDICTIONS"):
        add("elevated_jurisdiction", f"Elevated-risk jurisdiction: {jurisdiction}", WEIGHTS["elevated_jurisdiction"])

    if (entity_type or "").lower() != "individual" and ubo_verified is not True:
        add("corporate_ubo_unverified", "Corporate entity without verified UBO", WEIGHTS["corporate_ubo_unverified"])

    if overall_confidence is not None and overall_confidence < LOW_CONF_THRESHOLD:
        add("low_extraction_confidence", "Low document-extraction confidence", WEIGHTS["low_extraction_confidence"])

    if tx_summary:
        flagged = int(tx_summary.get("flagged_count") or 0)
        if flagged:
            add("tx_flagged", f"{flagged} flagged transaction(s)",
                min(flagged * WEIGHTS["tx_flagged_each"], WEIGHTS["tx_flagged_cap"]))
        if (tx_summary.get("max_risk_score") or 0) >= 80:
            add("tx_high_score", "Transaction with risk score >= 80", WEIGHTS["tx_high_score"])
        # Cross-customer counterparty links (network_detection). Dismissed / allowlisted links add nothing.
        if int(tx_summary.get("network_escalated") or 0):
            add("network_escalated", "Counterparty shared with other customers — escalated", WEIGHTS["network_escalated"])
        elif int(tx_summary.get("network_strong_open") or 0):
            add("network_strong_open", "Wallet shared with other customers — awaiting review", WEIGHTS["network_strong_open"])

    score = min(100, sum(f["points"] for f in factors))
    return score, band(score), factors


def summarise_transactions(rows: List[dict]) -> Dict[str, Any]:
    by_ccy: Dict[str, float] = {}
    flagged = 0
    max_rs = 0.0
    for r in rows:
        try:
            amt = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        ccy = (r.get("currency") or "UNK").upper()
        by_ccy[ccy] = round(by_ccy.get(ccy, 0.0) + amt, 2)
        if r.get("flag"):
            flagged += 1
        try:
            max_rs = max(max_rs, float(r.get("risk_score") or 0))
        except (TypeError, ValueError):
            pass
    return {"count": len(rows), "volume_by_currency": by_ccy, "flagged_count": flagged, "max_risk_score": max_rs}


def _pep_label(s: Dict[str, Any]) -> str:
    if s["state"] == "unscreened":
        return "NOT SCREENED"
    if s["pep"] and s["state"] in ("escalated", "needs_review"):
        return "MATCH — EDD Required"
    return "Pass"


def _sanctions_label(s: Dict[str, Any]) -> str:
    if s["state"] == "unscreened":
        return "NOT SCREENED"
    if s["sanctions"] and s["state"] in ("escalated", "needs_review"):
        return "MATCH — Escalated"
    return "Pass"


_CID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def _write_audit(sb, current: CurrentUser, event_type: str, detail: str, subject_id: str) -> None:
    """Hash-chained audit write, same pattern as extraction.py / cases.py."""
    tenant_id = str(current.tenant_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        prev = (sb.table("audit_events").select("hash").eq("tenant_id", tenant_id)
                .order("created_at", desc=True).limit(1).execute())
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
        sb.table("audit_events").insert({
            "tenant_id": tenant_id, "user_id": str(current.user_id), "created_by": current.sub,
            "event_type": event_type, "detail": detail,
            "hash": compute_event_hash(tenant_id, event_type, detail, previous_hash, now_iso),
            "previous_hash": previous_hash, "created_at": now_iso, "subject_id": subject_id,
        }).execute()
    except Exception as e:
        logger.error(f"AUDIT GAP: {event_type} failed for {subject_id}: {e!r}")
        try:
            sb.table("audit_events_failed").insert({
                "tenant_id": tenant_id, "user_id": str(current.user_id), "event_type": event_type,
                "detail": detail, "error": str(e), "occurred_at": now_iso,
            }).execute()
        except Exception:
            logger.critical(f"AUDIT GAP UNRECOVERABLE: {event_type} for {subject_id}")


def _load_extraction(sb, tenant_id: str, extraction_id: str) -> dict:
    res = (sb.table("extractions").select("*")
           .eq("id", extraction_id).eq("tenant_id", tenant_id).execute())
    if not res.data:
        raise HTTPException(404, f"Extraction {extraction_id} not found")
    return res.data[0]


def _derive_profile(extraction: dict) -> Dict[str, Any]:
    f = extraction.get("fields") or {}
    return {
        "full_name":     (f.get("full_name") or f.get("name") or "").strip(),
        "date_of_birth": (f.get("date_of_birth") or "").strip(),
        "nationality":   (f.get("nationality") or "").strip(),
    }


# ── Schemas ──────────────────────────────────────────────────────────────────

class OnboardRequest(BaseModel):
    extraction_id: str
    entity_type: str = "individual"           # individual | corporate | llc | other
    jurisdiction: str                          # confirmed by the user — nationality is NOT assumed
    ubo_verified: Optional[bool] = None        # only meaningful for non-individuals
    external_customer_id: Optional[str] = None # the id YOUR systems send on transaction webhooks
    full_name: Optional[str] = None            # optional correction of the extracted name


_ENTITY_TYPES = {"individual", "corporate", "llc", "other"}


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/onboarding-preview/{extraction_id}")
async def onboarding_preview(
    extraction_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    """Server-side view of what onboarding would create, derived from the STORED extraction."""
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    ext = _load_extraction(sb, tenant_id, extraction_id)
    prof = _derive_profile(ext)
    scr = classify_screening(ext.get("sanctions_result"))

    existing = (sb.table("customers").select("customer_id,risk_rating,risk_score")
                .eq("tenant_id", tenant_id).eq("extraction_id", extraction_id).execute())
    dupes = []
    if prof["full_name"]:
        d = (sb.table("customers").select("customer_id,full_name,jurisdiction")
             .eq("tenant_id", tenant_id).ilike("full_name", prof["full_name"]).limit(5).execute())
        dupes = d.data or []

    blocked_reason = None
    if scr["state"] == "unscreened":
        blocked_reason = "Sanctions/PEP screening did not run for this document. Re-run extraction or screen manually before onboarding."
    elif scr["state"] == "needs_review":
        blocked_reason = "A potential sanctions/PEP match has not been reviewed. Dismiss or escalate it first."
    elif not prof["full_name"]:
        blocked_reason = "No name was extracted from this document."

    score, rating, factors = score_customer(
        entity_type="individual", jurisdiction=prof["nationality"], ubo_verified=None,
        screening=scr, overall_confidence=ext.get("overall_confidence"))
    return {
        "extraction_id": extraction_id,
        "profile": prof,
        "screening": {
            "state": scr["state"],
            "pepScreening": _pep_label(scr),
            "sanctionsScreening": _sanctions_label(scr),
            "hit_count": len(scr["hits"]),
        },
        "risk_preview": {"risk_score": score, "risk_rating": rating, "factors": factors,
                         "note": "Preview only — final score also depends on the jurisdiction and entity type you confirm."},
        "already_onboarded": existing.data[0] if existing.data else None,
        "possible_duplicates": dupes,
        "can_onboard": blocked_reason is None and not existing.data,
        "blocked_reason": blocked_reason,
    }


@router.post("/onboard", status_code=201)
async def onboard_customer(
    payload: OnboardRequest,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    entity_type = (payload.entity_type or "").strip().lower()
    if entity_type not in _ENTITY_TYPES:
        raise HTTPException(422, f"entity_type must be one of {sorted(_ENTITY_TYPES)}")
    jurisdiction = (payload.jurisdiction or "").strip()
    if not jurisdiction:
        raise HTTPException(422, "jurisdiction is required")
    if payload.external_customer_id and not _CID_RE.match(payload.external_customer_id):
        raise HTTPException(422, "external_customer_id may contain letters, digits, . _ - only (max 64)")

    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    ext = _load_extraction(sb, tenant_id, payload.extraction_id)

    already = (sb.table("customers").select("customer_id")
               .eq("tenant_id", tenant_id).eq("extraction_id", payload.extraction_id).execute())
    if already.data:
        raise HTTPException(409, {"message": "This extraction has already been onboarded",
                                  "customer_id": already.data[0]["customer_id"]})

    prof = _derive_profile(ext)
    full_name = (payload.full_name or prof["full_name"]).strip()
    if not full_name:
        raise HTTPException(422, "No name available — provide full_name")

    scr = classify_screening(ext.get("sanctions_result"))
    if scr["state"] == "unscreened":
        raise HTTPException(409, "Sanctions/PEP screening did not run for this document — cannot onboard")
    if scr["state"] == "needs_review":
        raise HTTPException(409, "Unresolved sanctions/PEP match — dismiss or escalate it before onboarding")

    ubo = payload.ubo_verified if entity_type != "individual" else None
    score, rating, factors = score_customer(
        entity_type=entity_type, jurisdiction=jurisdiction, ubo_verified=ubo,
        screening=scr, overall_confidence=ext.get("overall_confidence"))

    customer_id = payload.external_customer_id or f"CUS-{uuid.uuid4().hex[:8].upper()}"
    taken = (sb.table("customers").select("id").eq("tenant_id", tenant_id)
             .eq("customer_id", customer_id).execute())
    if taken.data:
        raise HTTPException(409, f"customer_id {customer_id} already exists in this tenant")

    today = date.today()
    onboarding_status = "edd_required" if (scr["state"] == "escalated" or scr["pep"]) else "active"
    row = {
        "tenant_id": tenant_id, "customer_id": customer_id, "full_name": full_name,
        "entity_type": entity_type, "jurisdiction": jurisdiction,
        "risk_rating": rating, "risk_score": score, "risk_factors": factors,
        "ubo_verified": ubo,
        "kyc_review_date": today.isoformat(),
        "next_review_due": (today + timedelta(days=REVIEW_INTERVAL_DAYS[rating])).isoformat(),
        "pep_screening": _pep_label(scr), "sanctions_screening": _sanctions_label(scr),
        "date_of_birth": prof["date_of_birth"] or None, "nationality": prof["nationality"] or None,
        "extraction_id": payload.extraction_id, "onboarding_status": onboarding_status,
        "onboarded_by": current.sub, "source_system": "itica_onboarding",
        "external_id": customer_id,
    }
    try:
        res = sb.table("customers").insert(row).execute()
    except Exception as e:
        logger.error(f"customers insert failed for tenant {tenant_id}: {e!r}")
        raise HTTPException(502, "Could not create the customer record — nothing was saved")
    created = res.data[0] if res.data else row

    _write_audit(
        sb, current, "CUSTOMER_ONBOARDED",
        f"Customer {customer_id} onboarded from extraction {payload.extraction_id} | "
        f"Risk: {rating.upper()} ({score}) | Sanctions: {_sanctions_label(scr)} | PEP: {_pep_label(scr)} | "
        f"Status: {onboarding_status} | By: {current.sub}",
        customer_id,
    )
    return {"customer": created, "risk": {"risk_score": score, "risk_rating": rating, "factors": factors}}


@router.get("")
async def list_customers(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    sb = get_supabase()
    query = (sb.table("customers")
             .select("customer_id,full_name,entity_type,jurisdiction,risk_rating,risk_score,onboarding_status,kyc_review_date,next_review_due", count="exact")
             .eq("tenant_id", str(current.tenant_id)))
    if q:
        # PostgREST filter strings: keep the pattern simple/escaped.
        safe = re.sub(r"[^A-Za-z0-9 ._\-@]", "", q)
        if safe:
            query = query.ilike("full_name", f"%{safe}%")
    res = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return {"customers": res.data or [], "total": res.count or 0}


@router.get("/{customer_id}")
async def get_customer(
    customer_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    tx_limit: int = Query(100, ge=1, le=500),
):
    """Profile + every transaction linked to this customer_id (tenant-scoped)."""
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    c = (sb.table("customers").select("*").eq("tenant_id", tenant_id)
         .eq("customer_id", customer_id).execute())
    if not c.data:
        raise HTTPException(404, "Customer not found")
    tx = (sb.table("transactions")
          .select("external_id,occurred_at,amount,currency,counterparty,direction,flag,risk_score,source_system")
          .eq("tenant_id", tenant_id).eq("customer_id", customer_id)
          .order("occurred_at", desc=True).limit(tx_limit).execute())
    rows = tx.data or []
    return {"customer": c.data[0], "transactions": rows,
            "transaction_summary": summarise_transactions(rows),
            "transactions_truncated": len(rows) >= tx_limit}


@router.post("/{customer_id}/rescore")
async def rescore_customer(
    customer_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    """Recompute risk from the STORED screening + current transactions.
    Does not call the screening provider again (dilisense free tier is ~100 calls/month)."""
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    c = (sb.table("customers").select("*").eq("tenant_id", tenant_id)
         .eq("customer_id", customer_id).execute())
    if not c.data:
        raise HTTPException(404, "Customer not found")
    cust = c.data[0]

    overall, scr = None, {"state": "clear", "pep": False, "sanctions": False, "hits": []}
    if cust.get("extraction_id"):
        ext = _load_extraction(sb, tenant_id, cust["extraction_id"])
        overall = ext.get("overall_confidence")
        scr = classify_screening(ext.get("sanctions_result"))

    tx = (sb.table("transactions").select("amount,currency,flag,risk_score")
          .eq("tenant_id", tenant_id).eq("customer_id", customer_id).limit(1000).execute())
    summary = summarise_transactions(tx.data or [])
    summary["network_escalated"], summary["network_strong_open"] = 0, 0
    try:
        nl = sb.rpc("itica_network_links_for_customer", {"p_tenant": tenant_id, "p_customer": customer_id}).execute()
        for l in (nl.data or []):
            if l.get("status") == "escalated":
                summary["network_escalated"] += 1
            elif l.get("status") == "new" and l.get("kind") == "wallet":
                summary["network_strong_open"] += 1
    except Exception as e:   # migration 02 not run yet -> rescore still works without the network factor
        logger.warning(f"network link lookup skipped for {customer_id}: {e!r}")

    score, rating, factors = score_customer(
        entity_type=cust.get("entity_type") or "individual", jurisdiction=cust.get("jurisdiction") or "",
        ubo_verified=cust.get("ubo_verified"), screening=scr, overall_confidence=overall, tx_summary=summary)

    today = date.today()
    sb.table("customers").update({
        "risk_rating": rating, "risk_score": score, "risk_factors": factors,
        "next_review_due": (today + timedelta(days=REVIEW_INTERVAL_DAYS[rating])).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("tenant_id", tenant_id).eq("customer_id", customer_id).execute()

    _write_audit(sb, current, "CUSTOMER_RISK_RESCORED",
                 f"Customer {customer_id} rescored: {(cust.get('risk_rating') or '?').upper()} "
                 f"({cust.get('risk_score')}) -> {rating.upper()} ({score}) | Txns considered: {summary['count']} | By: {current.sub}",
                 customer_id)
    return {"customer_id": customer_id, "risk_score": score, "risk_rating": rating,
            "factors": factors, "transaction_summary": summary}
