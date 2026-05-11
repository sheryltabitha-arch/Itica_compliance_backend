"""
app/routers/export.py

Routes:
  GET /api/v1/export/kyc      — export KYC extractions (JSON or CSV)
  GET /api/v1/export/reports  — export compliance reports (JSON or CSV)

Auth: Bearer JWT  OR  itk_live_* API key.
API-key path looks up api_key_hash in tenant_integrations and verifies with bcrypt.
Updates api_key_last_used on each authenticated API-key request.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.middleware.auth import (
    CurrentUser,
    get_current_user,
    get_supabase,
    verify_auth0_token,
    get_or_create_user,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/export", tags=["export"])


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── dual auth helper ──────────────────────────────────────────────────────────

async def _resolve_user(request: Request) -> CurrentUser:
    """
    Resolve CurrentUser from either:
      1. Bearer JWT  (standard Auth0 token)
      2. itk_live_*  API key in Authorization header
    """
    auth_header: str = request.headers.get("Authorization", "")

    if auth_header.startswith("itk_live_"):
        # API-key path
        api_key = auth_header.strip()
        supabase = get_supabase()

        rows = (
            supabase.table("tenant_integrations")
            .select("tenant_id, api_key_hash, api_key_prefix")
            .execute()
        )
        if not rows.data:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

        matched_row = None
        prefix = api_key[:16]
        for row in rows.data:
            if row.get("api_key_prefix") == prefix:
                if bcrypt.checkpw(api_key.encode(), row["api_key_hash"].encode()):
                    matched_row = row
                    break

        if not matched_row:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

        tenant_id = matched_row["tenant_id"]

        # Update last used
        supabase.table("tenant_integrations").upsert({
            "tenant_id":         tenant_id,
            "api_key_last_used": _now_utc(),
            "updated_at":        _now_utc(),
        }, on_conflict="tenant_id").execute()

        # Fetch a real user record for this tenant to return a CurrentUser
        user_result = (
            supabase.table("users")
            .select("*, tenants(*)")
            .eq("tenant_id", tenant_id)
            .limit(1)
            .execute()
        )
        if not user_result.data:
            raise HTTPException(status_code=404, detail="No user found for tenant")

        return CurrentUser(user_result.data[0])

    elif auth_header.startswith("Bearer "):
        # Standard JWT path
        token = auth_header[7:]
        payload   = await verify_auth0_token(token)
        user_data = await get_or_create_user(payload)
        return CurrentUser(user_data, jwt_claims=payload)

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No valid authorization credential provided",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── KYC export ────────────────────────────────────────────────────────────────

@router.get("/kyc")
async def export_kyc(
    request:   Request,
    from_date: Optional[str] = Query(None, description="ISO date, e.g. 2024-01-01"),
    to_date:   Optional[str] = Query(None, description="ISO date, e.g. 2024-12-31"),
    format:    str            = Query("json", regex="^(json|csv)$"),
):
    current   = await _resolve_user(request)
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    query = (
        supabase.table("extractions")
        .select("*, kyc_documents(*)")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
    )
    if from_date:
        query = query.gte("created_at", from_date)
    if to_date:
        query = query.lte("created_at", to_date)

    result = query.execute()
    records = result.data or []

    if format == "csv":
        if not records:
            return StreamingResponse(
                io.StringIO(""),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=itica_kyc_export_{datetime.now(timezone.utc).date()}.csv"},
            )
        fieldnames = list(records[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=itica_kyc_export_{datetime.now(timezone.utc).date()}.csv"},
        )

    return {"records": records, "total": len(records), "tenant_id": tenant_id}


# ── reports export ────────────────────────────────────────────────────────────

@router.get("/reports")
async def export_reports(
    request:   Request,
    report_id: Optional[str] = Query(None),
    type:      Optional[str] = Query(None),
    period:    Optional[str] = Query(None),
    format:    str            = Query("json", regex="^(json|csv)$"),
):
    current   = await _resolve_user(request)
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    query = (
        supabase.table("reports")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
    )
    if report_id:
        query = query.eq("id", report_id)
    if type:
        query = query.eq("report_type", type)
    if period:
        query = query.eq("period", period)

    result  = query.execute()
    records = result.data or []

    if format == "csv":
        if not records:
            return StreamingResponse(
                io.StringIO(""),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=itica_reports_export_{datetime.now(timezone.utc).date()}.csv"},
            )
        fieldnames = list(records[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=itica_reports_export_{datetime.now(timezone.utc).date()}.csv"},
        )

    return {"records": records, "total": len(records), "tenant_id": tenant_id}
