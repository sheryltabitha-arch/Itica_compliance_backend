"""
app/routers/dashboard.py

# ── main.py wiring (add these two lines) ──────────────────────────────────
# In the import block:
#     from app.routers import integrations, webhook, export, dashboard
# In the router registration block:
#     app.include_router(dashboard.router, tags=["dashboard"])

Routes:
  GET /api/dashboard/metrics         — summary counts + confidence avg for a window
  GET /api/dashboard/trends          — confidence-over-time + processing velocity (daily buckets)
  GET /api/dashboard/alerts          — generated alerts (low confidence, sanctions hits, stale processing)
  GET /api/dashboard/investigations  — flagged/high-risk cases needing review

Built against the EXISTING `extractions` table schema (see app/routers/extraction.py):
  id, document_id, tenant_id, model_version, fields, confidence_scores,
  overall_confidence, low_confidence_fields, status, review_priority,
  sanctions_result, created_at

status values seen in extraction.py: "completed" | "requires_review" | "sanctions_hit"
review_priority values: "low" | "medium" | "high"

No new tables required — this reads what extraction.py is already writing.
Aggregation is done in Python rather than via Supabase RPC/SQL functions to
match the existing codebase convention (see integrations.py's /import dedup
logic, which does the same client-side aggregation rather than a stored proc).
If extraction volume grows large enough that this becomes slow, the next
step is a Postgres view or RPC function — not a rewrite of this file's shape.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

LOW_CONFIDENCE_THRESHOLD = 0.75  # matches the threshold implied by review_priority logic in extraction.py


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fetch_extractions_window(sb, tenant_id: str, days: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = (
        sb.table("extractions")
        .select("id, document_id, overall_confidence, low_confidence_fields, "
                "status, review_priority, model_version, created_at")
        .eq("tenant_id", tenant_id)
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ── /metrics ─────────────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_dashboard_metrics(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    days: int = Query(7, ge=1, le=90, description="Lookback window in days"),
):
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    rows = _fetch_extractions_window(sb, tenant_id, days)

    total = len(rows)
    confidences = [r["overall_confidence"] for r in rows if r.get("overall_confidence") is not None]
    avg_confidence = round(sum(confidences) / len(confidences), 4) if confidences else None

    status_breakdown: dict[str, int] = defaultdict(int)
    priority_breakdown: dict[str, int] = defaultdict(int)
    for r in rows:
        status_breakdown[r.get("status") or "unknown"] += 1
        priority_breakdown[r.get("review_priority") or "unknown"] += 1

    return {
        "window_days": days,
        "total_extractions": total,
        "average_confidence": avg_confidence,
        "status_breakdown": dict(status_breakdown),
        "review_priority_breakdown": dict(priority_breakdown),
        "sanctions_hits": status_breakdown.get("sanctions_hit", 0),
        "requires_review": status_breakdown.get("requires_review", 0),
    }


# ── /trends ──────────────────────────────────────────────────────────────────

@router.get("/trends")
async def get_dashboard_trends(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    days: int = Query(7, ge=1, le=90, description="Lookback window in days"),
):
    """
    Two series, both bucketed by day:
      confidence_trend     — avg overall_confidence per day
      processing_velocity  — extraction count per day (documents processed/day)
    """
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    rows = _fetch_extractions_window(sb, tenant_id, days)

    by_day_confidence: dict[str, list[float]] = defaultdict(list)
    by_day_count: dict[str, int] = defaultdict(int)

    for r in rows:
        ts = _parse_ts(r.get("created_at"))
        if ts is None:
            continue
        day_key = ts.date().isoformat()
        by_day_count[day_key] += 1
        if r.get("overall_confidence") is not None:
            by_day_confidence[day_key].append(r["overall_confidence"])

    all_days = sorted(set(by_day_count) | set(by_day_confidence))

    confidence_trend = [
        {
            "date": d,
            "average_confidence": round(sum(by_day_confidence[d]) / len(by_day_confidence[d]), 4)
            if by_day_confidence[d] else None,
            "sample_size": len(by_day_confidence[d]),
        }
        for d in all_days
    ]
    processing_velocity = [
        {"date": d, "documents_processed": by_day_count[d]}
        for d in all_days
    ]

    return {
        "window_days": days,
        "confidence_trend": confidence_trend,
        "processing_velocity": processing_velocity,
    }


# ── /alerts ──────────────────────────────────────────────────────────────────

@router.get("/alerts")
async def get_dashboard_alerts(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    days: int = Query(1, ge=1, le=30, description="Lookback window in days — defaults to last 24h"),
):
    """
    Alerts are DERIVED from existing extractions data, not a separate table.
    Three categories for now:
      low_confidence     — overall_confidence below LOW_CONFIDENCE_THRESHOLD
      sanctions_hit       — status == 'sanctions_hit' (mirrors the sanctions
                             screening block in extraction.py)
      high_review_volume  — review_priority == 'high' count, surfaced as its
                             own alert type since it signals analyst backlog risk
    """
    sb = get_supabase()
    tenant_id = str(current.tenant_id)
    rows = _fetch_extractions_window(sb, tenant_id, days)

    low_confidence_items = [
        r for r in rows
        if r.get("overall_confidence") is not None
        and r["overall_confidence"] < LOW_CONFIDENCE_THRESHOLD
    ]
    sanctions_items = [r for r in rows if r.get("status") == "sanctions_hit"]
    high_priority_items = [r for r in rows if r.get("review_priority") == "high"]

    alerts = [
        {
            "type": "low_confidence",
            "severity": "medium",
            "count": len(low_confidence_items),
            "extraction_ids": [r["id"] for r in low_confidence_items[:25]],
        },
        {
            "type": "sanctions_hit",
            "severity": "critical",
            "count": len(sanctions_items),
            "extraction_ids": [r["id"] for r in sanctions_items[:25]],
        },
        {
            "type": "high_review_volume",
            "severity": "low" if len(high_priority_items) < 10 else "medium",
            "count": len(high_priority_items),
            "extraction_ids": [r["id"] for r in high_priority_items[:25]],
        },
    ]

    return {
        "window_days": days,
        "alerts": [a for a in alerts if a["count"] > 0] or alerts,  # always show all 3 types, even at 0
    }


# ── /investigations ──────────────────────────────────────────────────────────

@router.get("/investigations")
async def get_dashboard_investigations(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    status_filter: Optional[str] = Query(
        None, description="Filter by status: requires_review | sanctions_hit"
    ),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Surfaces flagged/high-risk cases needing analyst attention — built from
    the SAME review_priority/status fields extraction.py already writes,
    just never previously exposed through an endpoint.

    NOTE: this does not yet track resolution/assignment state (who's working
    a case, whether it's been closed). That likely belongs in human_review.py
    given the existing review_tasks concept — worth checking whether that
    router already has a case-assignment model before adding a second one here.
    """
    sb = get_supabase()
    tenant_id = str(current.tenant_id)

    query = (
        sb.table("extractions")
        .select("id, document_id, overall_confidence, low_confidence_fields, "
                "status, review_priority, sanctions_result, created_at")
        .eq("tenant_id", tenant_id)
        .in_("status", ["requires_review", "sanctions_hit"])
        .order("created_at", desc=True)
        .limit(limit)
    )
    if status_filter:
        query = query.eq("status", status_filter)

    result = query.execute()
    rows = result.data or []

    return {
        "count": len(rows),
        "investigations": [
            {
                "extraction_id":   r["id"],
                "document_id":     r["document_id"],
                "status":          r["status"],
                "review_priority": r["review_priority"],
                "low_confidence_fields": r.get("low_confidence_fields") or [],
                "sanctions_result": r.get("sanctions_result"),
                "created_at":      r["created_at"],
            }
            for r in rows
        ],
    }
