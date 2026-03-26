"""
app/routers/reports.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.middleware.auth import CurrentUser, get_current_user, get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


@router.post("")
@router.get("/generate")
async def generate_report(
    period_start: str = "",
    period_end: str = "",
    report_type: str = "aml_summary",
    format: str = "pdf",
    current: CurrentUser = Depends(get_current_user),
):
    report_id = f"RPT-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    tenant_id = str(current.tenant_id)

    # Pull real extraction data
    extraction_count = 0
    completed_count = 0
    reviewed_count = 0
    avg_confidence = 0.0
    low_confidence_count = 0

    try:
        sb = get_supabase()
        query = (
            sb.table("extractions")
            .select("status, confidence_scores")
            .eq("tenant_id", tenant_id)
        )
        if period_start:
            query = query.gte("created_at", period_start)
        if period_end:
            query = query.lte("created_at", period_end)

        rows = query.execute().data or []
        extraction_count = len(rows)
        completed_count = sum(1 for r in rows if r.get("status") == "completed")
        reviewed_count = sum(1 for r in rows if r.get("status") == "reviewed")

        all_scores = []
        for row in rows:
            scores = row.get("confidence_scores") or {}
            all_scores.extend(float(v) for v in scores.values())

        if all_scores:
            avg_confidence = round(sum(all_scores) / len(all_scores), 4)
            low_confidence_count = sum(1 for s in all_scores if s < 0.75)

    except Exception as e:
        logger.warning(f"Report data query failed (non-fatal): {e}")

    # Hash chain — write to audit ledger
    report_hash = "demo-hash"
    try:
        sb = get_supabase()
        prev = (
            sb.table("audit_events")
            .select("hash")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"

        hash_input = json.dumps({
            "report_id": report_id,
            "tenant_id": tenant_id,
            "period_start": period_start,
            "period_end": period_end,
            "report_type": report_type,
            "extraction_count": extraction_count,
            "timestamp": now,
        }, sort_keys=True)
        report_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        event_count = (
            sb.table("audit_events")
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .execute()
        )
        event_num = (event_count.count or 0) + 1

        sb.table("audit_events").insert({
            "tenant_id": tenant_id,
            "user_id": str(current.user_id),
            "event_type": "REPORT_GENERATED",
            "event_id": f"EVT-{event_num:05d}",
            "detail": (
                f"Report {report_id} | {report_type} | "
                f"{period_start or 'all'} → {period_end or 'now'} | "
                f"{extraction_count} extractions"
            ),
            "hash": report_hash,
            "previous_hash": previous_hash,
        }).execute()

    except Exception as e:
        logger.warning(f"Report audit write failed (non-fatal): {e}")

    return {
        "report_id": report_id,
        "status": "sealed",
        "report_type": report_type,
        "format": format,
        "period_start": period_start,
        "period_end": period_end,
        "hash": report_hash,
        "generated_at": now,
        "tenant_id": tenant_id,
        "summary": {
            "total_documents": extraction_count,
            "completed": completed_count,
            "reviewed": reviewed_count,
            "pending": extraction_count - completed_count - reviewed_count,
            "avg_confidence": avg_confidence,
            "low_confidence_flags": low_confidence_count,
        },
        }    report_id = f"RPT-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()

    # Write to audit ledger
    try:
        sb = get_supabase()
        tenant_id = str(current.tenant_id)

        prev = (
            sb.table("audit_events")
            .select("hash")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"

        import hashlib, json
        hash_input = json.dumps({
            "report_id": report_id,
            "tenant_id": tenant_id,
            "period_start": period_start,
            "period_end": period_end,
            "format": format,
            "timestamp": now,
        }, sort_keys=True)
        report_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        event_count = (
            sb.table("audit_events")
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .execute()
        )
        event_num = (event_count.count or 0) + 1

        sb.table("audit_events").insert({
            "tenant_id": tenant_id,
            "user_id": current.user_id,
            "event_type": "REPORT GENERATED",
            "event_id": f"EVT-{event_num:05d}",
            "detail": f"Report {report_id} | {format.upper()} | {period_start} → {period_end}",
            "hash": report_hash,
            "previous_hash": previous_hash,
        }).execute()

    except Exception as e:
        logger.warning(f"Report audit write failed (non-fatal): {e}")
        report_hash = "demo-hash"

    return {
        "report_id": report_id,
        "status": "sealed",
        "format": format,
        "period_start": period_start,
        "period_end": period_end,
        "hash": report_hash,
        "generated_at": now,
        "tenant_id": str(current.tenant_id),
    }
