"""
app/models/permissions.py

Staff-only permission checks for actions that affect data ACROSS tenants,
not just within the caller's own tenant. Tenant-scoped role checks
(analyst/manager/admin within one tenant) already live in
app/middleware/auth.py (ROLE_HIERARCHY, require_role) — this file covers
the narrower, more sensitive case: Itica-staff-only actions, such as
writing regulatory_rules (shared across every tenant subscribed to that
regulation) or assigning a tenant's regulatory_framework at onboarding.

Configure real staff via the ITICA_STAFF_EMAILS env var (comma-separated).
Falls back to a single hardcoded address for local/dev use only — set the
env var on Render before treating this as a real access boundary.
"""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException, status

from app.middleware.auth import CurrentUser, get_current_user

_DEFAULT_STAFF_EMAILS = "iticatechinfo@gmail.com"


def _staff_emails() -> set[str]:
    raw = os.environ.get("ITICA_STAFF_EMAILS", _DEFAULT_STAFF_EMAILS)
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_itica_staff(current: CurrentUser) -> bool:
    return (current.email or "").lower() in _staff_emails()


async def require_itica_staff(
    current: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Use for endpoints that affect MULTIPLE tenants at once (e.g.
    regulatory_rules, shared by every tenant subscribed to that
    regulation). A tenant's own 'admin' role is NOT sufficient here —
    that only proves authority over their own tenant, not over shared
    platform-wide data."""
    if not is_itica_staff(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is restricted to Itica staff.",
        )
    return current
