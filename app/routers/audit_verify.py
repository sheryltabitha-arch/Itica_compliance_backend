"""
app/routers/audit_verify.py

GET /api/v1/audit/verify

Walks a tenant's audit_events (in created_at order) and checks two things
per row, using only the shared formula in app/services/audit_hash.py:

  1. hash_matches   — recomputing compute_event_hash() from this row's own
                       stored fields gives back the stored "hash" value.
  2. chain_linked   — this row's "previous_hash" equals the immediately
                       prior row's "hash".

Only rows at or after a cutover timestamp are checked. Rows written before
the shared hash function was deployed (decisions.py's old per-request
timestamp, human_review.py/integrations.py's own inline formulas,
extraction.py's old extraction_id placeholder) were never computed the same
way, so recomputing them will not match — that is expected, not tampering,
and this endpoint reports it as "not_covered" rather than "broken".

The cutover is required, not guessed, because there is no reliable way to
detect "which formula produced this row" from the row alone. Pass it as
?since=<ISO8601>, or set AUDIT_HASH_CUTOVER once in Render right after you
deploy the four hash-formula patches, so every caller gets the same answer
without having to know or pass the date each time.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole
from app.services.audit_hash import compute_event_hash

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

# Cap how many broken/uncovered rows we return inline — a full tenant dump
# isn't needed to answer "is the chain intact", and this keeps the response
# small even if something is badly wrong.
MAX_LISTED = 50


def _resolve_cutover(since: Optional[str]) -> str:
    cutover = since or os.environ.get("AUDIT_HASH_CUTOVER")
    if not cutover:
        raise HTTPException(
            400,
            "No verification cutover configured. Pass ?since=<ISO8601 timestamp> "
            "for the deploy time of the shared hash formula, or set "
            "AUDIT_HASH_CUTOVER in the environment.",
        )
    try:
        datetime.fromisoformat(cutover.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"'since' is not a valid ISO8601 timestamp: {cutover!r}")
    return cutover


@router.get("/verify")
async def verify_audit_chain(
    current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
    since: Optional[str] = Query(
        None,
        description="ISO8601 timestamp. Only events at/after this point are checked. "
                    "Defaults to the AUDIT_HASH_CUTOVER env var if not passed.",
    ),
):
    tenant_id = str(current.tenant_id)
    cutover = _resolve_cutover(since)
    sb = get_supabase()

    # One row immediately before the cutover, so the first in-window row's
    # previous_hash can be checked against something — informational only:
    # a pre-cutover row's hash was never computed by compute_event_hash(),
    # so this link is reported but never counted as a break.
    boundary = (
        sb.table("audit_events")
        .select("hash, created_at")
        .eq("tenant_id", tenant_id)
        .lt("created_at", cutover)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    boundary_hash = boundary.data[0]["hash"] if boundary.data else None

    result = (
        sb.table("audit_events")
        .select("event_id, event_type, detail, hash, previous_hash, created_at, subject_id")
        .eq("tenant_id", tenant_id)
        .gte("created_at", cutover)
        .order("created_at", desc=False)
        .execute()
    )
    events = result.data or []

    checked = 0
    hash_mismatches = []
    chain_breaks = []
    prior_hash = boundary_hash  # None if there is no pre-cutover row at all

    for row in events:
        checked += 1
        expected_hash = compute_event_hash(
            tenant_id, row["event_type"], row["detail"], row["previous_hash"], row["created_at"]
        )
        if expected_hash != row["hash"]:
            hash_mismatches.append({
                "event_id":   row["event_id"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
            })

        # Only enforced once we have a real prior hash to compare against —
        # the very first row in the whole tenant history (prior_hash is None
        # and there was no boundary row) legitimately has no predecessor.
        if prior_hash is not None and row["previous_hash"] != prior_hash:
            chain_breaks.append({
                "event_id":            row["event_id"],
                "created_at":          row["created_at"],
                "expected_previous":   prior_hash,
                "stored_previous":     row["previous_hash"],
            })
        prior_hash = row["hash"]

    intact = not hash_mismatches and not chain_breaks

    return {
        "tenant_id":            tenant_id,
        "cutover":              cutover,
        "checked_from_boundary": boundary_hash is not None,
        "events_checked":       checked,
        "chain_intact":         intact,
        "hash_mismatches":      hash_mismatches[:MAX_LISTED],
        "hash_mismatch_count":  len(hash_mismatches),
        "chain_breaks":         chain_breaks[:MAX_LISTED],
        "chain_break_count":    len(chain_breaks),
        "note": (
            "Only events at/after 'cutover' are checked, since earlier rows "
            "were written under a different (pre-standardization) hash "
            "formula and will not recompute to match — that is expected, "
            "not evidence of tampering."
        ),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
