"""
app/dependencies/tenant_rules.py

Shared dependency for regulation-aware behavior across decisions, audit,
and reports. A tenant's `regulatory_framework` (set manually at onboarding,
based on what the client tells us applies to them) may list MULTIPLE
regulations at once (e.g. AML + FATF + a country's central bank rules).
This dependency fetches all applicable `regulatory_rules` rows and merges
them: required fields are unioned, risk thresholds take the strictest
value across every applicable regulation.

Usage in a router:
    from app.dependencies.tenant_rules import get_tenant_rules

    @router.post("/")
    async def create_decision(
        payload: DecisionCreate,
        current: CurrentUser = Depends(get_current_user),
        tenant_rules: dict = Depends(get_tenant_rules),
    ):
        ...
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException

from app.middleware.auth import CurrentUser, get_current_user, get_supabase

logger = logging.getLogger(__name__)


async def get_tenant_rules(
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    if not tenant_id:
        raise HTTPException(status_code=400, detail="User has no tenant assigned")

    tenant_result = (
        supabase.table("tenants")
        .select("jurisdiction, regulatory_framework, report_template_id, dashboard_feature_flags")
        .eq("id", tenant_id)
        .execute()
    )
    tenant = tenant_result.data[0] if tenant_result.data else None

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    regulation_codes = tenant.get("regulatory_framework") or []

    merged_kyc_fields:     set = set()
    merged_audit_fields:   set = set()
    merged_report_sections: set = set()
    merged_thresholds:     dict = {}

    if regulation_codes:
        try:
            rules_result = (
                supabase.table("regulatory_rules")
                .select("*")
                .in_("regulation_code", regulation_codes)
                .execute()
            )
            rule_rows = rules_result.data or []
        except Exception as e:
            logger.error(
                f"Failed to load regulatory_rules for tenant {tenant_id}, "
                f"regulations {regulation_codes}: {e!r}"
            )
            rule_rows = []

        found_codes = {r["regulation_code"] for r in rule_rows}
        missing_codes = set(regulation_codes) - found_codes
        if missing_codes:
            # Not fatal — a regulation listed on the tenant but not yet encoded
            # in regulatory_rules just contributes nothing to the merge. Log it
            # so it doesn't go unnoticed at onboarding time.
            logger.warning(
                f"Tenant {tenant_id} lists regulation(s) {missing_codes} with no "
                f"matching regulatory_rules row — no rules applied for these."
            )

        for r in rule_rows:
            merged_kyc_fields.update(r.get("required_kyc_fields") or [])
            merged_audit_fields.update(r.get("mandatory_audit_fields") or [])
            merged_report_sections.update(r.get("report_sections_required") or [])
            for k, v in (r.get("risk_thresholds") or {}).items():
                # Strictest (lowest) threshold wins when a tenant is subject
                # to multiple regulations with different cutoffs for the same key.
                if k not in merged_thresholds or v < merged_thresholds[k]:
                    merged_thresholds[k] = v

    return {
        "tenant_id":                tenant_id,
        "jurisdiction":             tenant.get("jurisdiction"),
        "regulation_codes":         regulation_codes,
        "required_kyc_fields":      sorted(merged_kyc_fields),
        "risk_thresholds":          merged_thresholds,
        "mandatory_audit_fields":   sorted(merged_audit_fields),
        "report_sections_required": sorted(merged_report_sections),
        "report_template_id":      tenant.get("report_template_id") or "generic",
        "dashboard_feature_flags": tenant.get("dashboard_feature_flags") or {},
    }
