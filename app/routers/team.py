"""
app/routers/team.py

POST /api/team/members/invite — invite someone to the caller's tenant by email.

  - Senior Manager and above only (authority read from DB users.role, not the JWT).
  - Cannot grant a role above your own.
  - Stores a row in team_invites, writes a hash-chained audit event, emails the invitee.
  - Returns 201 with email_sent true/false. The invite is saved even if the email fails.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user, get_supabase
from app.services import invites as inv
from app.services.email_service import send_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/team", tags=["team"])


class InviteRequest(BaseModel):
    email: str
    role: str
    modules: Optional[List[str]] = None


@router.post("/members/invite", status_code=201)
async def invite_member(payload: InviteRequest, current: CurrentUser = Depends(get_current_user)):
    caller_tier = inv.tier_of(str(current.get("role") or ""))
    if caller_tier < inv.MANAGE_TIER:
        raise HTTPException(403, "This requires Senior Manager access or above.")

    email = inv.normalise_email(payload.email)
    if not email:
        raise HTTPException(422, "Enter a valid email address.")

    key = (payload.role or "").strip().lower()
    if key not in inv.ASSIGNABLE_ROLES:
        raise HTTPException(422, f"role must be one of {inv.ASSIGNABLE_ROLES}")
    if inv.tier_of(key) > caller_tier:
        raise HTTPException(403, "You can't assign a role above your own.")
    stored_role = inv.to_stored_role(key)

    modules = []
    for m in (payload.modules or [])[:20]:
        m = str(m).strip()[:40]
        if m and m not in modules:
            modules.append(m)

    tenant_id = str(current.tenant_id)
    sb = get_supabase()

    existing = (sb.table("users").select("id,tenant_id")
                .ilike("email", inv.ilike_exact(email)).execute()).data or []
    if existing:
        if any(str(u.get("tenant_id")) == tenant_id for u in existing):
            raise HTTPException(409, "That person is already a member.")
        raise HTTPException(409, "That email already has an Itica account.")

    try:
        open_rows = (sb.table("team_invites").select("*").eq("email", email)
                     .eq("status", "pending").execute()).data or []
        reinvite = None
        for row in open_rows:
            if str(row["tenant_id"]) == tenant_id:
                reinvite = row
            elif inv.is_expired(row):
                sb.table("team_invites").update({"status": "revoked"}).eq("id", row["id"]).execute()
            else:
                raise HTTPException(409, "That email already has a pending invitation from another organisation.")

        if reinvite is None:
            n = len(sb.table("team_invites").select("id").eq("tenant_id", tenant_id)
                    .eq("status", "pending").execute().data or [])
            if n >= inv.MAX_PENDING_PER_TENANT:
                raise HTTPException(429, f"You have {n} open invitations. Revoke some first.")

        row = {"role": stored_role, "modules": modules, "expires_at": inv.new_expiry_iso(),
               "invited_by": str(current.user_id), "invited_by_name": current.name or current.email}
        if reinvite:
            res = sb.table("team_invites").update(row).eq("id", reinvite["id"]).execute()
            invite = {**reinvite, **row, **(res.data[0] if res.data else {})}
        else:
            res = sb.table("team_invites").insert(
                {**row, "tenant_id": tenant_id, "email": email, "status": "pending"}).execute()
            invite = res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        if any(t in str(e) for t in ("team_invites", "PGRST205", "42P01")):
            logger.error(f"team_invites table missing — run migrations/team_invites.sql: {e!r}")
            raise HTTPException(503, "Invitations aren't set up on the server yet (team_invites table missing).")
        logger.exception("Invite save failed")
        raise HTTPException(500, "Couldn't save the invitation. Nothing was changed.")

    inv.write_audit(sb, tenant_id, str(current.user_id), current.sub, "TEAM_MEMBER_INVITED",
                    f"Invited {email} as {inv.role_label(stored_role)} · by {current.name or current.email}",
                    str(invite["id"]))

    tname = ""
    try:
        t = sb.table("tenants").select("name").eq("id", tenant_id).execute()
        tname = (t.data[0].get("name") if t.data else "") or ""
    except Exception:
        pass
    msg = inv.build_invite_email(to_email=email, inviter_name=current.name, inviter_email=current.email,
                                 org_name=tname, stored_role=stored_role)
    sent, err = await send_email(email, msg["subject"], msg["html"], msg["text"], reply_to=current.email or None)
    if sent:
        try:
            sb.table("team_invites").update({"last_sent_at": inv.now_utc().isoformat(),
                                             "send_count": int(invite.get("send_count") or 0) + 1}
                                            ).eq("id", invite["id"]).execute()
        except Exception:
            pass

    return {"id": str(invite["id"]), "email": email, "role": stored_role, "status": "invited",
            "expires_at": invite.get("expires_at"), "email_sent": sent, "email_error": err}
