"""
app/routers/customer.py

Admin interface for the manual, onboarding-driven regulation assignment
described in planning: a client tells us which regulations apply to them
and shares their report layout; we translate that into rows here.

⚠️ SECURITY NOTE — READ BEFORE DEPLOYING
regulatory_rules is SHARED across every tenant on the platform (keyed by
regulation_code, not by tenant). Write access here must be restricted to
Itica staff, NOT any client's own "admin" user, because one tenant's admin
could otherwise change required-field rules that other tenants depend on.

This file currently gates writes on `current.role == "admin"`, matching the
role naming seen in the original schema draft. This is a placeholder until
auth.py is reviewed and a proper staff-only distinction exists (e.g. a
separate `is_staff` flag, or checking against a known internal tenant_id).
Do not treat `role == "admin"` as sufficient isolation for this file's
write endpoints in production without that follow-up.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user, get_supabase, ROLE_HIERARCHY

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/customer", tags=["customer"])


def _require_admin(current: CurrentUser):
    # See security note above — this check needs to become staff-only,
    # not merely "any tenant's admin", before this file is safe to expose
    # beyond Itica's own internal use.
    if current.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


# ── Schemas ─────────────────────────────────────────────────────────────────

class TenantConfigUpdate(BaseModel):
    jurisdiction:             str | None = None
    regulatory_framework:     list[str] | None = None   # e.g. ["FATF", "CBK_KE"]
    report_template_id:       str | None = None
    dashboard_feature_flags:  dict | None = None


class RegulatoryRuleUpsert(BaseModel):
    regulation_code:            str
    display_name:                str | None = None
    required_kyc_fields:         list[str] = []
    risk_thresholds:              dict = {}
    mandatory_audit_fields:       list[str] = []
    report_sections_required:     list[str] = []


# ── Tenant config: read (own tenant) ─────────────────────────────────────────

@router.get("/tenant-config")
async def get_tenant_config(current: CurrentUser = Depends(get_current_user)):
    """Returns the calling user's own tenant's regulatory configuration."""
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    result = (
        supabase.table("tenants")
        .select("id, name, jurisdiction, regulatory_framework, report_template_id, dashboard_feature_flags")
        .eq("id", tenant_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return result.data[0]


# ── Tenant config: write (staff-gated, see security note) ───────────────────

@router.patch("/tenant-config")
async def update_tenant_config(
    payload: TenantConfigUpdate,
    current: CurrentUser = Depends(get_current_user),
):
    """Assigns a tenant's applicable regulations and report layout.
    This is the onboarding step: the client tells us what applies to them
    and which layout they shared, and we set it here."""
    _require_admin(current)
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    result = (
        supabase.table("tenants")
        .update(update_data)
        .eq("id", tenant_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Tenant not found")

    logger.info(f"Tenant config updated for {tenant_id} by {current.sub}: {update_data}")
    return result.data[0]


# ── Regulatory rules: read (any authenticated user, needed to render forms) ─

@router.get("/regulatory-rules")
async def list_regulatory_rules(current: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = supabase.table("regulatory_rules").select("*").execute()
    return result.data or []


@router.get("/regulatory-rules/{regulation_code}")
async def get_regulatory_rule(regulation_code: str, current: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("regulatory_rules")
        .select("*")
        .eq("regulation_code", regulation_code)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail=f"No rules found for regulation '{regulation_code}'")
    return result.data[0]


# ── Regulatory rules: write (staff-gated, see security note) ────────────────

@router.post("/regulatory-rules")
async def upsert_regulatory_rule(
    payload: RegulatoryRuleUpsert,
    current: CurrentUser = Depends(get_current_user),
):
    """Creates or updates the rule set for one regulation. This affects
    EVERY tenant currently assigned this regulation_code, not just the
    caller's own tenant — write access must stay staff-restricted."""
    _require_admin(current)
    supabase = get_supabase()

    result = (
        supabase.table("regulatory_rules")
        .upsert(payload.model_dump(), on_conflict="regulation_code")
        .execute()
    )

    logger.info(f"regulatory_rules upserted for '{payload.regulation_code}' by {current.sub}")
    return result.data[0] if result.data else payload.model_dump()
