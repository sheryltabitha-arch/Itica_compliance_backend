"""
app/routers/audit.py

Audit trail endpoints — tenant-isolated, tamper-evident.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from app.middleware.auth import get_current_user, get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/")
async def get_audit_trail(
    limit: int = Query(50, le=200),
    offset: int = 0,
    event_type: str | None = None,
    search: str | None = None,
    user: dict = Depends(get_current_user),
):
    """
    Return audit events for current tenant only.
    Supports filtering by type and full-text search.
    """
    supabase = get_supabase()
    tenant_id = str(user.get("tenant_id", ""))

    query = supabase.table("audit_events") \
        .select("*") \
        .eq("tenant_id", tenant_id) \
        .order("created_at", desc=True)

    if event_type:
        query = query.eq("event_type", event_type)

    result = query.range(offset, offset + limit - 1).execute()

    # Filter by search client-side (Supabase free tier doesn't have full-text search)
    events = result.data
    if search:
        s = search.lower()
        events = [
            e for e in events
            if s in (e.get("event_id") or "").lower()
            or s in (e.get("detail") or "").lower()
            or s in (e.get("hash") or "").lower()
        ]

    # Verify chain integrity
    integrity = "VERIFIED"
    for i in range(1, len(events)):
        if events[i]["hash"] != events[i-1].get("previous_hash"):
            integrity = "WARNING"
            break

    count_result = supabase.table("audit_events") \
        .select("id", count="exact") \
        .eq("tenant_id", tenant_id) \
        .execute()

    return {
        "events": events,
        "total": count_result.count or 0,
        "integrity": integrity,
        "tenant_id": tenant_id,
    }
