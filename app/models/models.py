"""
app/models/models.py

CLEANED — see audit trail below before assuming anything here is unused.

KEPT:
  UserRole         — actively imported by app/routers/extraction.py,
                      app/routers/human_review.py, app/routers/dashboard.py
                      for require_min_role(). This is load-bearing.
  AuditActionType   — kept defensively; not confirmed dead, cheap to keep.
  User              — kept defensively; SQLAlchemy mirror of the `users`
                      Supabase table. Not confirmed to be queried via this
                      ORM class anywhere, but also not confirmed dead.
                      Re-run: grep -rn "models.User\\b\\|from app.models.models import User" .
                      before removing.

REMOVED (confirmed dead via grep across the full repo):
  ExtractionResult, ExtractionCorrection, ReviewTask, ModelRegistry — never
    imported outside this file.
  AuditEvent — was imported ONLY by app/services/audit_ledger.py, which
    itself never committed anything to a DB (db.add() with no commit, and
    get_db() requires DATABASE_URL which isn't set in this Supabase-only
    deployment). audit_ledger.py is being deleted in this same change —
    see DELETIONS.md. With it gone, AuditEvent has zero callers.
  DriftAlert — a DIFFERENT, unrelated plain class with the same name is
    defined directly in app/services/drift_monitor.py and is what's
    actually used. This SQLAlchemy version was always dead and was a
    confusing naming collision.

If you add new live ORM usage later, re-add Base/declarative_base() — Base
is removed here since nothing in the kept classes needs it (User doesn't
currently subclass it either; see note below).
"""
from __future__ import annotations
from datetime import datetime
from enum import Enum


class UserRole(str, Enum):
    user = "user"
    compliance_officer = "compliance_officer"
    admin = "admin"


class AuditActionType(str, Enum):
    document_uploaded = "document_uploaded"
    extraction_requested = "extraction_requested"
    extraction_completed = "extraction_completed"
    correction_submitted = "correction_submitted"
    review_completed = "review_completed"
    report_generated = "report_generated"
    user_logged_in = "user_logged_in"
    user_logged_out = "user_logged_out"


# NOTE: User is kept as a plain reference/dataclass-style shape rather than
# a SQLAlchemy model, since the SQLAlchemy engine/session path (app/db/session.py)
# is unreachable in this Supabase-only deployment (DATABASE_URL unset) — see
# DELETIONS.md. If you confirm via grep that User is never instantiated or
# queried via SQLAlchemy anywhere, this class can be deleted too. Left as a
# plain class (not Base-backed) so it can't accidentally be added to a
# SQLAlchemy session and silently no-op the way AuditEvent did.
class User:
    def __init__(
        self,
        id: str,
        email: str,
        tenant_id: str,
        name: str | None = None,
        picture: str | None = None,
        role: str = "user",
        email_verified: bool = False,
        last_login: datetime | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ):
        self.id = id
        self.email = email
        self.tenant_id = tenant_id
        self.name = name
        self.picture = picture
        self.role = role
        self.email_verified = email_verified
        self.last_login = last_login
        self.created_at = created_at or datetime.utcnow()
        self.updated_at = updated_at or datetime.utcnow()
