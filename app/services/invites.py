"""
app/services/invites.py

Shared logic for team invitations:
  - role vocabulary and tier checks (mirrors the frontend's ROLE_TIERS)
  - hash-chained audit writes
  - the invite email itself

"""
from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.services.audit_hash import compute_event_hash

logger = logging.getLogger(__name__)

CLAIM_NS = "https://iticacompliance.com"
INVITE_TTL_DAYS = 14
MAX_PENDING_PER_TENANT = 25
RESEND_COOLDOWN_SECONDS = 60

# Same ladder as the frontend (ROLE_TIERS in the app). Unknown roles fail closed.
ROLE_TIERS: Dict[str, int] = {
    "viewer": 0, "auditor": 0,
    "analyst": 1, "investigator": 1, "user": 1,
    "officer": 2, "compliance_officer": 2,
    "manager": 3, "senior_manager": 3, "director": 3,
    "admin": 4, "head_of_compliance": 4, "cco": 4,
    "chief_compliance_officer": 4, "founder": 4, "co_founder": 4,
}
MANAGE_TIER = 3  # Senior Manager and above can invite / change roles

# Keys the invite dropdown may send.
ASSIGNABLE_ROLES = ["auditor", "analyst", "investigator", "officer", "manager", "admin"]

ROLE_LABELS = {
    "auditor": "External Auditor", "analyst": "Compliance Analyst",
    "investigator": "Investigator", "officer": "Compliance Officer",
    "compliance_officer": "Compliance Officer",
    "manager": "Senior Manager", "admin": "Admin / CCO",
}
ROLE_BLURBS = {
    "auditor": "read-only access to the audit trail so you can review compliance records",
    "analyst": "view and flag cases, and read the dashboard and reports",
    "investigator": "full case access: investigate, add notes and escalate cases",
    "officer": "file SARs, manage and close cases, and export audit reports",
    "manager": "everything a Compliance Officer can do, plus managing the team",
    "admin": "full access, including billing, integrations and user management",
}

_EMAIL_RE = re.compile(r"^[^\s@,;<>()\"']+@[^\s@,;<>()\"']+\.[^\s@,;<>()\"']{2,}$")


def tier_of(role: Optional[str]) -> int:
    k = re.sub(r"[\s\-]+", "_", str(role or "").strip().lower())
    return ROLE_TIERS.get(k, 0)


def to_stored_role(key: str) -> str:
    """The existing backend gates routes on compliance_officer / manager / admin
    (ROLE_HIERARCHY in middleware/auth.py), so 'officer' is stored under its
    backend name. Every other key is stored as-is."""
    return "compliance_officer" if key == "officer" else key


def role_label(stored_role: str) -> str:
    return ROLE_LABELS.get(stored_role) or str(stored_role).replace("_", " ").title()


def normalise_email(raw: str) -> Optional[str]:
    e = (raw or "").strip().lower()
    return e if len(e) <= 254 and _EMAIL_RE.match(e) else None


def ilike_exact(value: str) -> str:
    """Escape LIKE wildcards so ilike() behaves as a case-insensitive equals."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def is_expired(invite: Dict[str, Any]) -> bool:
    exp = parse_ts(invite.get("expires_at"))
    return bool(exp and exp < now_utc())


def new_expiry_iso() -> str:
    return (now_utc() + timedelta(days=INVITE_TTL_DAYS)).isoformat()


# ── audit ───────────────────────────────────────────────────────────────────

def write_audit(sb, tenant_id: str, user_id: str, created_by: str,
                event_type: str, detail: str, subject_id: str) -> None:
    """Hash-chained audit write — same pattern as cases.py / customer_onboarding.py."""
    tenant_id = str(tenant_id)
    created_at = now_utc().isoformat()
    try:
        prev = (sb.table("audit_events").select("hash").eq("tenant_id", tenant_id)
                .order("created_at", desc=True).limit(1).execute())
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
        sb.table("audit_events").insert({
            "tenant_id": tenant_id, "user_id": str(user_id), "created_by": created_by,
            "event_type": event_type, "detail": detail,
            "hash": compute_event_hash(tenant_id, event_type, detail, previous_hash, created_at),
            "previous_hash": previous_hash, "created_at": created_at, "subject_id": subject_id,
        }).execute()
    except Exception as e:
        logger.error(f"AUDIT GAP: {event_type} failed for {subject_id}: {e!r}")
        try:
            sb.table("audit_events_failed").insert({
                "tenant_id": tenant_id, "user_id": str(user_id), "event_type": event_type,
                "detail": detail, "error": str(e), "occurred_at": created_at,
            }).execute()
        except Exception:
            logger.critical(f"AUDIT GAP UNRECOVERABLE: {event_type} for {subject_id}")


# ── email ───────────────────────────────────────────────────────────────────

def app_url() -> str:
    return os.environ.get("APP_URL", "https://www.iticacompliance.com").rstrip("/")


def build_invite_email(*, to_email: str, inviter_name: str, inviter_email: str,
                       org_name: str, stored_role: str) -> Dict[str, str]:
    key = "officer" if stored_role == "compliance_officer" else stored_role
    label = role_label(stored_role)
    blurb = ROLE_BLURBS.get(key, "access to the modules your team has set up for you")
    url = app_url()
    first = (to_email.split("@")[0] or "there")
    first = re.sub(r"[._\-]+", " ", first).split(" ")[0].title()
    inviter = inviter_name or inviter_email or "A colleague"
    org = org_name or "their team"
    subject = f"{inviter} invited you to join {org} on Itica"

    text = "\n".join([
        f"Hi {first},", "",
        f"{inviter} has invited you to join {org} on Itica, the compliance platform that keeps "
        "KYC checks, case investigations and decisions in one auditable record.", "",
        f"Your role: {label} — {blurb}.", "",
        "To get started:",
        f"1. Open {url}",
        f"2. Select Sign In and use this email address ({to_email}) so your invitation is matched to your account.",
        "3. Verify your email if asked. Your role and access are applied automatically.", "",
        f"This invitation expires in {INVITE_TTL_DAYS} days. If you weren't expecting it, you can ignore this email"
        + (f" or reply to {inviter_email}." if inviter_email else "."), "",
        "Welcome aboard,", "The Itica team", "iticacompliance.com",
    ])

    e = html.escape
    page = f"""<!doctype html><html><body style="margin:0;background:#f4f4ef;font-family:Georgia,serif;color:#111">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4ef;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden">
<tr><td style="background:#111;padding:20px 28px;color:#c8f135;font-size:18px;font-weight:700;letter-spacing:.04em">ITICA</td></tr>
<tr><td style="padding:28px">
<p style="margin:0 0 14px;font-size:16px">Hi {e(first)},</p>
<p style="margin:0 0 14px;font-size:15px;line-height:1.6"><strong>{e(inviter)}</strong> has invited you to join <strong>{e(org)}</strong> on Itica, the compliance platform that keeps KYC checks, case investigations and decisions in one auditable record.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:18px 0;background:#f4f4ef;border-radius:8px;width:100%"><tr><td style="padding:14px 16px;font-size:14px;line-height:1.55">
<div style="font-size:11px;letter-spacing:.12em;color:#777;font-weight:700;margin-bottom:4px">YOUR ROLE</div>
<strong>{e(label)}</strong><br/>{e(blurb[0].upper() + blurb[1:])}.</td></tr></table>
<p style="margin:0 0 20px"><a href="{e(url)}" style="display:inline-block;background:#111;color:#c8f135;text-decoration:none;font-weight:700;font-size:15px;padding:13px 26px;border-radius:8px">Accept invitation</a></p>
<p style="margin:0 0 6px;font-size:14px;line-height:1.6">Sign in with <strong>{e(to_email)}</strong> so your invitation is matched to your account. If asked, verify your email first. Your role and access are applied automatically.</p>
<p style="margin:18px 0 0;font-size:12px;color:#777;line-height:1.6">This invitation expires in {INVITE_TTL_DAYS} days. If you weren't expecting it, you can ignore this email{(' or reply to ' + e(inviter_email)) if inviter_email else ''}.</p>
</td></tr></table>
<p style="font-size:11px;color:#999;margin:14px 0 0">Itica &middot; iticacompliance.com</p>
</td></tr></table></body></html>"""
    return {"subject": subject, "text": text, "html": page}
