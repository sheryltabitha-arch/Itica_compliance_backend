"""
app/routers/audit_gaps.py

GET /api/v1/audit/gaps

Every router in this codebase that writes to audit_events wraps the insert
in a try/except that falls back to audit_events_failed on failure (see
human_review.py's "AUDIT GAP" logger lines for the pattern this follows).
That fallback means a failed write doesn't crash the request, but it does
leave a real hole in the audit trail's chain, and nothing currently
surfaces that hole to a compliance officer.

This endpoint lists what's in audit_events_failed for the tenant, so gaps
are visible and actionable rather than sitting silently in Render logs
until someone happens to grep for "AUDIT GAP".

LIMITATION: I don't know the exact column list of audit_events_failed
beyond what human_review.py's insert shows (tenant_id, user_id, event_type,
detail, error, occurred_at) — this selects "*" so it returns whatever
columns actually exist rather than guessing at more. If the table has a
"resolved" or "retried" column that this doesn't reference, send the
schema and I'll add proper resolve/retry actions on top of this.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("/gaps")
async def list_audit_gaps(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
):
    tenant_id = str(current.tenant_id)
    sb = get_supabase()

    rows = (
        sb.table("audit_events_failed")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("occurred_at", desc=True)
        .execute()
        .data or []
    )

    return {
        "tenant_id":   tenant_id,
        "gap_count":   len(rows),
        "gaps":        rows,
        "note": (
            "Each row here is a compliance decision or extraction whose "
            "audit_events write failed at the time it happened — the "
            "underlying action still occurred, but its evidence entry is "
            "missing from the hash chain. These should be reviewed and, "
            "where possible, backfilled as a labeled correction event "
            "(never inserted as if they'd happened on time)."
        ) if rows else "No audit write failures recorded for this tenant.",
    }
