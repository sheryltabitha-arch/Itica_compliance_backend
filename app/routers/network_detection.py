"""
app/routers/network_detection.py

Within-tenant "cross-network" pattern detection:

    Customer A -> transaction X  \
    Customer B -> transaction Y   >-- Wallet Z  => Itica links A and B through Z
    Customer C -> transaction W  /

Detection itself runs in Postgres (itica_network_scan, see migrations/02_network_detection.sql)
so it is one indexed query instead of pulling every transaction into Python. This router adds
the workflow around it: scan, list, drill-down, dismiss / escalate (-> case), allowlist.

Scope and limits (be honest about these in due-diligence answers)
  * ONE tenant only. It never compares across tenants.
  * A wallet/address match is a strong signal; a free-text NAME match ("Nairobi Freight Co") is weak.
  * Matches are only as good as the counterparty field on the transaction webhooks.
  * Shared counterparties are often innocent (exchanges, payroll, big merchants). Use the allowlist.
  * A link is an INDICATOR for human review — never an automatic accusation or a SAR decision.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_supabase, require_min_role
from app.models.models import UserRole
from app.routers.customer_onboarding import _write_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/network", tags=["network-detection"])

_STATUSES = {"new", "dismissed", "escalated", "allowlisted"}
_SEVERITIES = {"low", "medium", "high"}
MIN_REASON = 10


def _uuid_or_404(v: str) -> str:
    try:
        return str(uuid.UUID(v))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, "Not found")


def _load_link(sb, tenant_id: str, link_id: str) -> dict:
    res = (sb.table("network_links").select("*")
           .eq("id", _uuid_or_404(link_id)).eq("tenant_id", tenant_id).execute())
    if not res.data:
        raise HTTPException(404, "Network link not found")
    return res.data[0]


def _safe_ids(ids: List[str]) -> List[str]:
    return [i for i in ids if isinstance(i, str) and i]


class Disposition(BaseModel):
    action: str            # dismiss | escalate
    reason: str


class AllowlistRequest(BaseModel):
    reason: str


@router.post("/scan")
async def scan_network(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    min_customers: int = Query(2, ge=2, le=20),
    days: int = Query(180, ge=1, le=1095),
):
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    try:
        res = sb.rpc("itica_network_scan",
                     {"p_tenant": tenant_id, "p_min_customers": min_customers, "p_days": days}).execute()
    except Exception as e:
        logger.error(f"itica_network_scan failed for tenant {tenant_id}: {e!r}")
        raise HTTPException(502, "Network scan failed — has migration 02_network_detection.sql been run?")
    links = res.data or []
    open_links = [l for l in links if l.get("status") == "new"]
    _write_audit(sb, current, "NETWORK_SCAN",
                 f"Network scan: {len(links)} shared-counterparty link(s), {len(open_links)} awaiting review "
                 f"| min_customers={min_customers} window={days}d | By: {current.sub}",
                 f"network-scan-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    return {"scanned_window_days": days, "min_customers": min_customers,
            "total": len(links), "awaiting_review": len(open_links), "links": links}


@router.get("/links")
async def list_links(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    status: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    if status and status not in _STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(_STATUSES)}")
    if severity and severity not in _SEVERITIES:
        raise HTTPException(422, f"severity must be one of {sorted(_SEVERITIES)}")
    sb = get_supabase()
    q = sb.table("network_links").select("*").eq("tenant_id", str(current.tenant_id))
    if status:
        q = q.eq("status", status)
    if severity:
        q = q.eq("severity", severity)
    rows = q.order("last_seen", desc=True).limit(limit).execute().data or []
    if not status:
        rows = [r for r in rows if r.get("status") != "allowlisted"]
    rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (r.get("status") != "new", rank.get(r.get("severity"), 3)))
    return {"links": rows, "total": len(rows)}


@router.get("/links/{link_id}")
async def get_link(
    link_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    tx_limit: int = Query(200, ge=1, le=500),
):
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    link = _load_link(sb, tenant_id, link_id)
    cids = _safe_ids(link.get("customer_ids") or [])
    cust = []
    if cids:
        cust = (sb.table("customers")
                .select("customer_id,full_name,entity_type,jurisdiction,risk_rating,risk_score")
                .eq("tenant_id", tenant_id).in_("customer_id", cids).execute().data or [])
    tx = (sb.table("transactions")
          .select("external_id,customer_id,occurred_at,amount,currency,direction,flag,counterparty")
          .eq("tenant_id", tenant_id).eq("counterparty_norm", link["counterparty_norm"])
          .order("occurred_at", desc=True).limit(tx_limit).execute().data or [])
    return {"link": link, "customers": cust, "transactions": tx,
            "transactions_truncated": len(tx) >= tx_limit}


@router.get("/customer/{customer_id}")
async def links_for_customer(
    customer_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    sb = get_supabase()
    try:
        res = sb.rpc("itica_network_links_for_customer",
                     {"p_tenant": str(current.tenant_id), "p_customer": customer_id}).execute()
    except Exception as e:
        logger.error(f"links_for_customer failed: {e!r}")
        raise HTTPException(502, "Network lookup failed — has migration 02_network_detection.sql been run?")
    return {"customer_id": customer_id, "links": res.data or []}


def _try_open_case(sb, current: CurrentUser, link: dict, reason: str) -> Optional[str]:
    """Best-effort suspicious_transaction case. Escalation must still succeed if the cases table is absent."""
    ref = f"NET-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    try:
        res = sb.table("customer_cases").insert({
            "tenant_id": str(current.tenant_id), "case_reference": ref,
            "customer_name": None, "customer_id": (link.get("customer_ids") or [None])[0],
            "case_type": "suspicious_transaction", "status": "open", "assigned_to": None,
            "notes": f"Shared counterparty across {link.get('customer_count')} customers: "
                     f"{link.get('counterparty_display')} — {reason}",
            "metadata": {"source": "network_detection", "network_link_id": link["id"],
                         "counterparty": link.get("counterparty_display"),
                         "customer_ids": link.get("customer_ids")},
            "created_by": current.user_id, "decision_ids": [], "kyc_document_ids": [],
        }).execute()
        return ref if res.data else None
    except Exception as e:
        logger.error(f"network escalation: case creation failed for link {link.get('id')}: {e!r}")
        return None


@router.post("/links/{link_id}/disposition")
async def disposition(
    link_id: str, payload: Disposition,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    action = (payload.action or "").strip().lower()
    reason = (payload.reason or "").strip()
    if action not in ("dismiss", "escalate"):
        raise HTTPException(422, "action must be 'dismiss' or 'escalate'")
    if len(reason) < MIN_REASON:
        raise HTTPException(422, f"A reason of at least {MIN_REASON} characters is required")
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    link = _load_link(sb, tenant_id, link_id)
    if link["status"] == "escalated":
        raise HTTPException(409, "This link has already been escalated")
    if link["status"] == "allowlisted":
        raise HTTPException(409, "This counterparty is allowlisted")
    if action == "dismiss" and link["status"] == "dismissed":
        raise HTTPException(409, "This link has already been dismissed")

    now = datetime.now(timezone.utc).isoformat()
    update: Dict[str, Any] = {
        "status": "dismissed" if action == "dismiss" else "escalated",
        "disposition": {"action": action, "reason": reason, "by": current.sub, "at": now},
        "updated_at": now,
    }
    case_ref = None
    if action == "escalate":
        case_ref = _try_open_case(sb, current, link, reason)
        if case_ref:
            update["case_reference"] = case_ref
    sb.table("network_links").update(update).eq("id", link["id"]).eq("tenant_id", tenant_id).execute()
    _write_audit(sb, current,
                 "NETWORK_LINK_DISMISSED" if action == "dismiss" else "NETWORK_LINK_ESCALATED",
                 f"Shared counterparty {link.get('counterparty_display')} ({link.get('kind')}, "
                 f"{link.get('customer_count')} customers: {', '.join(_safe_ids(link.get('customer_ids') or []))}) "
                 f"{action}d | Reason: {reason} | Case: {case_ref or 'none'} | By: {current.sub}",
                 link["id"])
    return {"id": link["id"], "status": update["status"], "case_reference": case_ref,
            "case_created": bool(case_ref) if action == "escalate" else None}


@router.post("/links/{link_id}/allowlist")
async def allowlist_link(
    link_id: str, payload: AllowlistRequest,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    reason = (payload.reason or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(422, f"A reason of at least {MIN_REASON} characters is required")
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    link = _load_link(sb, tenant_id, link_id)
    if link["status"] == "escalated":
        raise HTTPException(409, "An escalated link cannot be allowlisted")
    sb.table("network_allowlist").insert({
        "tenant_id": tenant_id, "counterparty_norm": link["counterparty_norm"],
        "reason": reason, "created_by": current.sub}).execute()
    sb.table("network_links").update({"status": "allowlisted", "updated_at": datetime.now(timezone.utc).isoformat()}
                                     ).eq("id", link["id"]).eq("tenant_id", tenant_id).execute()
    _write_audit(sb, current, "NETWORK_COUNTERPARTY_ALLOWLISTED",
                 f"Counterparty {link.get('counterparty_display')} allowlisted — will no longer be linked | "
                 f"Reason: {reason} | By: {current.sub}", link["id"])
    return {"id": link["id"], "status": "allowlisted"}


@router.get("/allowlist")
async def list_allowlist(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    sb = get_supabase()
    rows = (sb.table("network_allowlist").select("id,counterparty_norm,reason,created_by,created_at")
            .eq("tenant_id", str(current.tenant_id)).order("created_at", desc=True).limit(500).execute().data or [])
    return {"allowlist": rows}


@router.delete("/allowlist/{entry_id}")
async def remove_allowlist(
    entry_id: str,
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    eid = _uuid_or_404(entry_id)
    found = sb.table("network_allowlist").select("*").eq("id", eid).eq("tenant_id", tenant_id).execute()
    if not found.data:
        raise HTTPException(404, "Allowlist entry not found")
    sb.table("network_allowlist").delete().eq("id", eid).eq("tenant_id", tenant_id).execute()
    _write_audit(sb, current, "NETWORK_ALLOWLIST_REMOVED",
                 f"Allowlist entry for {found.data[0]['counterparty_norm']} removed — the next scan will re-link it | By: {current.sub}",
                 eid)
    return {"id": eid, "removed": True}
