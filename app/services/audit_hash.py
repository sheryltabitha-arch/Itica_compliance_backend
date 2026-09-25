"""
app/services/audit_hash.py

Single hash formula for every audit_events insert. Every input must be a
value that is also persisted on the row itself (tenant_id, event_type,
detail, previous_hash, created_at) — so a later verifier can recompute this
same hash from nothing but what's stored, without needing to know which
router originally wrote the row.

Do not add a field to this formula unless that field is also saved as its
own column or is fully reconstructable from `detail`. A hash that includes
an ephemeral value (e.g. a timestamp generated but never stored) can never
be verified later — this was the actual bug in decisions.py.
"""
from __future__ import annotations
import hashlib


def compute_event_hash(tenant_id: str, event_type: str, detail: str,
                        previous_hash: str, created_at_iso: str) -> str:
    payload = f"{tenant_id}|{event_type}|{detail}|{previous_hash}|{created_at_iso}"
    return hashlib.sha256(payload.encode()).hexdigest()
