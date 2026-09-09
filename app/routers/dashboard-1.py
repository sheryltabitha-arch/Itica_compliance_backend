"""
app/routers/dashboard.py

Backs the three Compliance Intel panels that previously rendered as
static, hardcoded HTML in index.html: Regulatory Watch, Team Efficiency,
and Overdue Compliance Tasks.

Data model notes:
- regulatory_updates is a GLOBAL feed (not tenant-scoped) — the same
  regulatory news applies across every tenant in a given jurisdiction.
  It's matched to a tenant at read time via the tenant's `jurisdiction`
  field, not duplicated per tenant.
- compliance_tasks IS tenant-scoped. Its first real source of truth is
  decisions.py: any decision flagged sar_required auto-creates a task
  here (see create_decision in decisions.py). Tasks can also be created
  directly via POST for anything outside that flow.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user, get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ── Regulatory Watch ──────────────────────────────────────────────────────

@router.get("/regulatory-watch")
async def get_regulatory_watch(current: CurrentUser = Depends(get_current_user)):
    """Returns regulatory updates relevant to the calling user's tenant
    jurisdiction, plus anything tagged GLOBAL (e.g. FATF statements)."""
    supabase = get_supabase()

    tenant_row = (
        supabase.table("tenants")
        .select("jurisdiction")
        .eq("id", str(current.tenant_id))
        .execute()
    )
    jurisdiction = (tenant_row.data[0].get("jurisdiction") if tenant_row.data else None) or "GLOBAL"

    result = (
        supabase.table("regulatory_updates")
        .select("*")
        .in_("jurisdiction_code", [jurisdiction, "GLOBAL"])
        .order("issued_date", desc=True)
        .limit(20)
        .execute()
    )
    return {"updates": result.data or [], "jurisdiction": jurisdiction}


# ── Compliance Tasks (backs both Team Efficiency and Overdue Tasks) ──────

class TaskCreate(BaseModel):
    title:                  str
    description:            str | None = None
    assigned_officer_id:    str | None = None
    assigned_officer_name:  str | None = None
    due_date:               str  # ISO 8601
    priority:               str = "medium"
    reference_id:           str | None = None


@router.post("/tasks")
async def create_task(payload: TaskCreate, current: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("compliance_tasks")
        .insert({
            "tenant_id":              str(current.tenant_id),
            "title":                  payload.title,
            "description":            payload.description,
            "assigned_officer_id":    payload.assigned_officer_id,
            "assigned_officer_name":  payload.assigned_officer_name,
            "due_date":               payload.due_date,
            "priority":               payload.priority,
            "reference_id":           payload.reference_id,
            "source":                 "manual",
        })
        .execute()
    )
    return result.data[0]


@router.patch("/tasks/{task_id}/complete")
async def complete_task(task_id: str, current: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("compliance_tasks")
        .update({"status": "complete", "completed_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", task_id)
        .eq("tenant_id", str(current.tenant_id))
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Task not found for this tenant")
    return result.data[0]


@router.get("/overdue-tasks")
async def get_overdue_tasks(current: CurrentUser = Depends(get_current_user)):
    supabase = get_supabase()
    now_iso  = datetime.now(timezone.utc).isoformat()

    result = (
        supabase.table("compliance_tasks")
        .select("*")
        .eq("tenant_id", str(current.tenant_id))
        .neq("status", "complete")
        .lt("due_date", now_iso)
        .order("due_date", desc=False)
        .execute()
    )
    tasks = result.data or []
    for t in tasks:
        due = datetime.fromisoformat(t["due_date"].replace("Z", "+00:00"))
        t["days_overdue"] = max((datetime.now(timezone.utc) - due).days, 0)

    return {"tasks": tasks, "total": len(tasks)}


@router.get("/team-efficiency")
async def get_team_efficiency(current: CurrentUser = Depends(get_current_user)):
    """Aggregates compliance_tasks per officer for this tenant. Officers
    with zero tasks assigned don't appear — there's nothing to report on."""
    supabase = get_supabase()
    tenant_id = str(current.tenant_id)

    result = (
        supabase.table("compliance_tasks")
        .select("*")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    tasks = result.data or []

    by_officer: dict[str, dict] = {}
    now = datetime.now(timezone.utc)

    for t in tasks:
        officer_id = t.get("assigned_officer_id") or "unassigned"
        name       = t.get("assigned_officer_name") or "Unassigned"
        bucket = by_officer.setdefault(officer_id, {
            "officer_id": officer_id,
            "officer_name": name,
            "total": 0,
            "completed": 0,
            "overdue": 0,
            "_close_days": [],
        })
        bucket["total"] += 1

        due = datetime.fromisoformat(t["due_date"].replace("Z", "+00:00"))
        if t["status"] == "complete":
            bucket["completed"] += 1
            if t.get("completed_at"):
                created  = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                closed   = datetime.fromisoformat(t["completed_at"].replace("Z", "+00:00"))
                bucket["_close_days"].append((closed - created).total_seconds() / 86400)
        elif due < now:
            bucket["overdue"] += 1

    officers = []
    for bucket in by_officer.values():
        close_days = bucket.pop("_close_days")
        avg_close  = round(sum(close_days) / len(close_days), 1) if close_days else None
        pct        = round((bucket["completed"] / bucket["total"]) * 100) if bucket["total"] else 0
        status     = "OVERLOADED" if bucket["overdue"] >= 3 else ("ON CALL" if pct >= 90 else "ACTIVE")
        officers.append({
            **bucket,
            "completion_pct": pct,
            "avg_close_days": avg_close,
            "status": status,
        })

    officers.sort(key=lambda o: o["total"], reverse=True)
    return {"officers": officers, "tenant_id": tenant_id}
