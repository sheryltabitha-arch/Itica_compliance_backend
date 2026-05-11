"""
app/routers/integrations.py

Routes:
  POST /api/integrations/webhook/generate  — generate & store webhook secret
  POST /api/integrations/apikey/generate   — generate & store hashed API key
  GET  /api/integrations/status            — return integration health/metadata
  POST /api/integrations/import            — bulk import from Unit21 / Sardine / custom

Requires tenant_integrations table with columns:
  tenant_id (PK / unique), webhook_secret, webhook_last_ping,
  api_key_hash, api_key_prefix, api_key_last_rotated, api_key_last_used,
  subscription_status, plan, subscription_id, updated_at
"""
from __future__ import annotations

import csv
import io
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.middleware.auth import CurrentUser, get_current_user, get_supabase, require_role

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations", tags=["integrations"])

BACKEND_URL = "https://itica-compliance-backend.onrender.com"


# ── helpers ───────────────────────────────────────────────────────────────────

def _map_risk_score(score: int | float | str) -> str:
    """Map numeric risk score to Itica risk tier string."""
    try:
        s = int(float(score))
    except (ValueError, TypeError):
        return str(score)  # already a string tier — pass through
    if s <= 39:
        return "Low"
    if s <= 69:
        return "Medium"
    if s <= 89:
        return "High"
    return "Critical"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── webhook generate ──────────────────────────────────────────────────────────

@router.post("/webhook/generate")
async def generate_webhook(current: CurrentUser = Depends(get_current_user)):
    supabase      = get_supabase()
    tenant_id     = str(current.tenant_id)
    webhook_secret = secrets.token_urlsafe(32)

    supabase.table("tenant_integrations").upsert({
        "tenant_id":      tenant_id,
        "webhook_secret": webhook_secret,
        "updated_at":     _now_utc(),
    }, on_conflict="tenant_id").execute()

    return {
        "webhook_url": f"{BACKEND_URL}/api/webhook/ingest/{tenant_id}",
    }


# ── api key generate ──────────────────────────────────────────────────────────

@router.post("/apikey/generate")
async def generate_api_key(current: CurrentUser = Depends(get_current_user)):
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    key    = "itk_live_" + secrets.token_urlsafe(24)
    prefix = key[:16]
    hashed = bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()

    supabase.table("tenant_integrations").upsert({
        "tenant_id":            tenant_id,
        "api_key_hash":         hashed,
        "api_key_prefix":       prefix,
        "api_key_last_rotated": _now_utc(),
        "updated_at":           _now_utc(),
    }, on_conflict="tenant_id").execute()

    # Plaintext returned once only — not stored
    return {"api_key": key, "prefix": prefix}


# ── integrations status ───────────────────────────────────────────────────────

@router.get("/status")
async def integrations_status(current: CurrentUser = Depends(get_current_user)):
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    result = (
        supabase.table("tenant_integrations")
        .select("*")
        .eq("tenant_id", tenant_id)
        .execute()
    )

    if not result.data:
        return {
            "webhook_active":      False,
            "api_key_active":      False,
            "webhook_last_ping":   None,
            "api_key_last_used":   None,
            "subscription_status": current.subscription_status,
            "plan":                current.plan,
        }

    row  = result.data[0]
    now  = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    def _within_24h(ts: str | None) -> bool:
        if not ts:
            return False
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt > cutoff
        except ValueError:
            return False

    return {
        "webhook_active":      _within_24h(row.get("webhook_last_ping")),
        "api_key_active":      _within_24h(row.get("api_key_last_used")),
        "webhook_last_ping":   row.get("webhook_last_ping"),
        "api_key_last_used":   row.get("api_key_last_used"),
        "subscription_status": row.get("subscription_status", current.subscription_status),
        "plan":                row.get("plan", current.plan),
    }


# ── historical import ─────────────────────────────────────────────────────────

@router.post("/import")
async def import_historical(
    source: str        = Form(...),
    file:   UploadFile = File(...),
    current: CurrentUser = Depends(require_role("manager")),
):
    """
    Bulk import audit records from Unit21, Sardine, or a custom CSV/JSON file.
    Deduplicates on (tenant_id, original_timestamp, reference_id).
    """
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    raw = (await file.read()).decode("utf-8", errors="replace")

    # ── parse ─────────────────────────────────────────────────────────────────
    filename = (file.filename or "").lower()
    rows: list[dict] = []

    if filename.endswith(".json"):
        data = json.loads(raw)
        rows = data if isinstance(data, list) else [data]
    else:
        # Default to CSV
        reader = csv.DictReader(io.StringIO(raw))
        rows   = list(reader)

    if not rows:
        raise HTTPException(status_code=400, detail="File contains no rows")

    # ── field mapping ─────────────────────────────────────────────────────────
    def map_row(row: dict) -> dict:
        if source == "unit21":
            return {
                "id":                 row.get("alert_id",       str(uuid.uuid4())),
                "reference_id":       row.get("entity_id",      ""),
                "risk_tier":          _map_risk_score(row.get("risk_score", 0)),
                "original_timestamp": row.get("created_at",     ""),
                "rationale":          row.get("analyst_notes",  ""),
            }
        if source == "sardine":
            return {
                "id":                 row.get("session_id",     str(uuid.uuid4())),
                "reference_id":       row.get("user_id",        ""),
                "risk_tier":          _map_risk_score(row.get("score", 0)),
                "original_timestamp": row.get("timestamp",      ""),
                "rationale":          None,
            }
        # custom — expect columns matching Itica schema directly
        return row

    mapped = [map_row(r) for r in rows]

    # ── deduplication — fetch existing (ref_id, original_timestamp) pairs ─────
    existing_refs = set()
    existing_result = (
        supabase.table("audit_events")
        .select("detail, created_at")
        .eq("tenant_id", tenant_id)
        .eq("import_source", source)
        .execute()
    )
    # Store as (reference_id, date-prefix) tuples — lightweight check
    for ev in (existing_result.data or []):
        existing_refs.add(ev.get("detail", ""))

    batch_id   = str(uuid.uuid4())
    imported   = 0
    skipped    = 0
    timestamps = []

    to_insert = []
    for mr in mapped:
        ref_id   = mr.get("reference_id", "")
        orig_ts  = mr.get("original_timestamp", "")
        dedup_key = f"{ref_id}|{orig_ts}"

        if dedup_key in existing_refs:
            skipped += 1
            continue

        if orig_ts:
            try:
                timestamps.append(datetime.fromisoformat(orig_ts.replace("Z", "+00:00")))
            except ValueError:
                pass

        to_insert.append({
            "tenant_id":        tenant_id,
            "created_by":       current.sub,
            "event_type":       "IMPORTED_RECORD",
            "detail":           dedup_key,
            "import_source":    source,
            "import_batch_id":  batch_id,
            "imported_at":      _now_utc(),
            "risk_tier":        mr.get("risk_tier"),
            "reference_id":     ref_id,
            "rationale":        mr.get("rationale"),
            "hash":             "",          # non-native imports — no chain hash
            "previous_hash":    "",
        })

    if to_insert:
        # Supabase recommends batches ≤ 500 rows
        chunk_size = 500
        for i in range(0, len(to_insert), chunk_size):
            supabase.table("audit_events").insert(to_insert[i:i + chunk_size]).execute()
        imported = len(to_insert)

    min_date = min(timestamps).date().isoformat() if timestamps else "unknown"
    max_date = max(timestamps).date().isoformat() if timestamps else "unknown"

    # Summary event
    supabase.table("audit_events").insert({
        "tenant_id":       tenant_id,
        "created_by":      current.sub,
        "event_type":      "IMPORT",
        "detail": (
            f"Historical import: {imported} records from {source}, "
            f"covering {min_date}–{max_date}. Batch #{batch_id}"
        ),
        "import_source":   source,
        "import_batch_id": batch_id,
        "imported_at":     _now_utc(),
        "hash":            "",
        "previous_hash":   "",
    }).execute()

    return {
        "records_imported":  imported,
        "duplicates_skipped": skipped,
        "batch_id":          batch_id,
        "date_range":        {"from": min_date, "to": max_date},
    }
