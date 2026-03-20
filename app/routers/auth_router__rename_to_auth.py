"""
app/routers/auth.py
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.middleware.auth import get_current_user, get_supabase

logger = logging.getLogger(__name__)
router = APIRouter()


class UserProfile(BaseModel):
    user_id: str
    email: str
    name: str
    picture: str | None
    tenant_id: str
    tenant_name: str
    role: str
    plan: str
    member_since: str


@router.get("/verify", response_model=UserProfile)
async def verify_token(request: Request, user=Depends(get_current_user)):
    supabase = get_supabase()
    try:
        supabase.table("user_sessions").insert({
            "user_id": user["id"],
            "tenant_id": user.get("tenant_id"),
            "ip_address": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to log session: {e}")

    tenant = user.get("tenants") or {}
    return UserProfile(
        user_id=user["id"],
        email=user["email"],
        name=user.get("name") or user["email"],
        picture=user.get("picture"),
        tenant_id=str(user.get("tenant_id", "")),
        tenant_name=tenant.get("name", "My Organisation"),
        role=user.get("role", "analyst"),
        plan=tenant.get("plan", "demo"),
        member_since=_format_date(user.get("created_at")),
    )


@router.get("/me", response_model=UserProfile)
async def get_me(user=Depends(get_current_user)):
    tenant = user.get("tenants") or {}
    return UserProfile(
        user_id=user["id"],
        email=user["email"],
        name=user.get("name") or user["email"],
        picture=user.get("picture"),
        tenant_id=str(user.get("tenant_id", "")),
        tenant_name=tenant.get("name", "My Organisation"),
        role=user.get("role", "analyst"),
        plan=tenant.get("plan", "demo"),
        member_since=_format_date(user.get("created_at")),
    )


@router.post("/logout")
async def logout(user=Depends(get_current_user)):
    supabase = get_supabase()
    try:
        supabase.table("audit_events").insert({
            "tenant_id": user.get("tenant_id"),
            "user_id": user["id"],
            "event_type": "USER LOGOUT",
            "event_id": f"EVT-LOGOUT-{user['id'][:8]}",
            "detail": f"User {user['email']} logged out",
            "hash": "logout-event",
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to log logout: {e}")
    return {"status": "logged_out"}


def _format_date(dt_str: str | None) -> str:
    if not dt_str:
        return datetime.now().strftime("%b %Y")
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%b %Y")
    except Exception:
        return datetime.now().strftime("%b %Y")
