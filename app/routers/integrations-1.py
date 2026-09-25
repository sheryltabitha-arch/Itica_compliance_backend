"""
app/routers/integrations.py

Routes:
  POST /api/integrations/webhook/generate       — legacy: single webhook secret per tenant
  POST /api/integrations/apikey/generate         — generate & store hashed API key
  GET  /api/integrations/status                  — legacy single-connection health/metadata
  POST /api/integrations/import                   — bulk import from Unit21 / Sardine / custom (manual file upload)

  POST /api/integrations/connect                  — NEW: connect a vendor via the connector framework
  GET  /api/integrations/connections               — NEW: list all per-vendor connections + sync status
  POST /api/integrations/{vendor}/backfill/resume   — NEW: continue a partial backfill

Legacy routes use tenant_integrations (one secret per tenant_id).
New routes use integration_connections (one row per tenant_id + vendor) —
see migrations/002_integration_connections.sql.
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
import hashlib
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user, get_supabase, require_role
from app.services.integrations.crypto import encrypt_credentials
from app.services.integrations.registry import get_connector_class, list_available_vendors
from app.services.integrations.sync_service import run_backfill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations", tags=["integrations"])

BACKEND_URL = "https://itica-compliance-backend.onrender.com"
BACKFILL_PAGES_PER_INVOCATION = 50  # time-budget cap per background-task run


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


# ── webhook generate (legacy single-secret) ────────────────────────────────────

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


# ── integrations status (legacy single-connection) ─────────────────────────────

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


# ── historical import (manual CSV/JSON upload) ──────────────────────────────────

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

    filename = (file.filename or "").lower()
    rows: list[dict] = []

    if filename.endswith(".json"):
        data = json.loads(raw)
        rows = data if isinstance(data, list) else [data]
    else:
        reader = csv.DictReader(io.StringIO(raw))
        rows   = list(reader)

    if not rows:
        raise HTTPException(status_code=400, detail="File contains no rows")

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
        return row

    mapped = [map_row(r) for r in rows]

    existing_refs = set()
    existing_result = (
        supabase.table("audit_events")
        .select("detail, created_at")
        .eq("tenant_id", tenant_id)
        .eq("import_source", source)
        .execute()
    )
    for ev in (existing_result.data or []):
        existing_refs.add(ev.get("detail", ""))

    # Continue the tenant's existing hash chain rather than leaving imported
    # records unlinked (empty hash/previous_hash) — unlinked records make
    # audit.py's full-chain integrity check report a false WARNING for any
    # tenant that has ever run an import.
    tip = (
        supabase.table("audit_events")
        .select("hash")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    chain_hash = tip.data[0]["hash"] if tip.data else "GENESIS"

    # Synthetic strictly-increasing timestamps: a single bulk INSERT can
    # evaluate NOW() once for the whole statement, which would let multiple
    # rows share created_at and make the DB-returned order ambiguous versus
    # the hash-chain order we build here. Explicit per-row timestamps avoid
    # depending on that ordering being preserved.
    import_base_time = datetime.now(timezone.utc)

    batch_id   = str(uuid.uuid4())
    imported   = 0
    skipped    = 0
    timestamps = []

    to_insert = []
    for idx, mr in enumerate(mapped):
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

        from app.services.audit_hash import compute_event_hash
        previous_hash = chain_hash
        _record_created_at = (import_base_time + timedelta(microseconds=idx)).isoformat()
        record_hash = compute_event_hash(
            tenant_id, "IMPORTED_RECORD", dedup_key, previous_hash, _record_created_at
        )
        chain_hash = record_hash  # next imported record links to this one

        to_insert.append({
            "tenant_id":        tenant_id,
            "created_by":       current.sub,
            "event_type":       "IMPORTED_RECORD",
            "detail":           dedup_key,
            "import_source":    source,
            "import_batch_id":  batch_id,
            "imported_at":      _now_utc(),
            "created_at":       _record_created_at,
            "risk_tier":        mr.get("risk_tier"),
            "reference_id":     ref_id,
            "rationale":        mr.get("rationale"),
            "hash":             record_hash,
            "previous_hash":    previous_hash,
            "subject_id":       ref_id or None,
        })

    if to_insert:
        chunk_size = 500
        for i in range(0, len(to_insert), chunk_size):
            supabase.table("audit_events").insert(to_insert[i:i + chunk_size]).execute()
        imported = len(to_insert)

    min_date = min(timestamps).date().isoformat() if timestamps else "unknown"
    max_date = max(timestamps).date().isoformat() if timestamps else "unknown"

    from app.services.audit_hash import compute_event_hash
    summary_previous_hash = chain_hash
    summary_detail = (
        f"Historical import: {imported} records from {source}, "
        f"covering {min_date}–{max_date}. Batch #{batch_id}"
    )
    summary_created_at = (import_base_time + timedelta(seconds=1)).isoformat()
    summary_hash = compute_event_hash(
        tenant_id, "IMPORT", summary_detail, summary_previous_hash, summary_created_at
    )

    supabase.table("audit_events").insert({
        "tenant_id":       tenant_id,
        "created_by":      current.sub,
        "event_type":      "IMPORT",
        "detail":          summary_detail,
        "import_source":   source,
        "import_batch_id": batch_id,
        "imported_at":     _now_utc(),
        "created_at":      summary_created_at,
        "hash":            summary_hash,
        "previous_hash":   summary_previous_hash,
        "subject_id":      batch_id,
    }).execute()

    return {
        "records_imported":  imported,
        "duplicates_skipped": skipped,
        "batch_id":          batch_id,
        "date_range":        {"from": min_date, "to": max_date},
    }


# ══════════════════════════════════════════════════════════════════════════════
# NEW: connector-framework routes (per-vendor, supports any tool incl. Fireblocks)
# ══════════════════════════════════════════════════════════════════════════════

class ConnectIntegrationRequest(BaseModel):
    vendor: str
    credentials: dict
    sync_direction: str = "inbound"  # inbound | outbound | bidirectional


@router.post("/connect")
async def connect_integration(
    body: ConnectIntegrationRequest,
    background_tasks: BackgroundTasks,
    current: CurrentUser = Depends(require_role("manager")),
):
    """
    Connects a new vendor for this tenant via the connector framework:
      1. Validates the vendor is supported (registry lookup — 400 if not)
      2. Calls connector.authenticate() to fail fast on bad credentials
      3. Upserts integration_connections with a per-vendor webhook secret
      4. Kicks off full-history backfill as a background task

    Backfill runs capped at BACKFILL_PAGES_PER_INVOCATION pages per call —
    if backfill_status comes back 'in_progress', call
    POST /{vendor}/backfill/resume to continue from the persisted cursor.
    """
    tenant_id = str(current.tenant_id)
    vendor = body.vendor

    try:
        connector_cls = get_connector_class(vendor)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported vendor '{vendor}'. Available: {list_available_vendors()}",
        )

    connector = connector_cls(tenant_id, body.credentials)
    try:
        authenticated = await connector.authenticate()
    except Exception as e:
        logger.warning(f"Authentication failed for tenant={tenant_id} vendor={vendor}: {e}")
        raise HTTPException(status_code=401, detail=f"Could not authenticate with {vendor}: {e}")

    if not authenticated:
        raise HTTPException(status_code=401, detail=f"Authentication rejected by {vendor}")

    webhook_secret = secrets.token_urlsafe(32)
    supabase = get_supabase()

    upsert_result = (
        supabase.table("integration_connections")
        .upsert({
            "tenant_id":       tenant_id,
            "vendor":          vendor,
            "credentials":     encrypt_credentials(body.credentials),  # encrypted at rest — see crypto.py
            "webhook_secret":  webhook_secret,
            "sync_direction":  body.sync_direction,
            "backfill_status": "not_started",
            "active":          True,
            "updated_at":      _now_utc(),
        }, on_conflict="tenant_id,vendor")
        .execute()
    )
    connection_id = upsert_result.data[0]["id"] if upsert_result.data else None

    background_tasks.add_task(
        run_backfill, tenant_id, vendor, BACKFILL_PAGES_PER_INVOCATION
    )

    return {
        "vendor": vendor,
        "connection_id": connection_id,
        "webhook_url": f"{BACKEND_URL}/api/webhook/ingest/{tenant_id}/{vendor}",
        "backfill_status": "started",
        "sync_direction": body.sync_direction,
        "supports_outbound": connector.supports_outbound,
    }


@router.get("/connections")
async def list_integration_connections(current: CurrentUser = Depends(get_current_user)):
    """Per-vendor status — the multi-vendor equivalent of /status above."""
    supabase  = get_supabase()
    tenant_id = str(current.tenant_id)

    result = (
        supabase.table("integration_connections")
        .select("vendor, sync_direction, backfill_status, backfill_completed_at, "
                "last_synced_at, last_sync_error, active, created_at")
        .eq("tenant_id", tenant_id)
        .execute()
    )

    return {"connections": result.data or []}


@router.post("/{vendor}/backfill/resume")
async def resume_backfill(
    vendor: str,
    background_tasks: BackgroundTasks,
    current: CurrentUser = Depends(require_role("manager")),
):
    tenant_id = str(current.tenant_id)
    supabase  = get_supabase()

    existing = (
        supabase.table("integration_connections")
        .select("backfill_status")
        .eq("tenant_id", tenant_id)
        .eq("vendor", vendor)
        .execute()
    )
    if not existing.data:
        raise HTTPException(404, f"No connection found for vendor '{vendor}'")
    if existing.data[0]["backfill_status"] == "completed":
        return {"status": "already_completed", "vendor": vendor}

    background_tasks.add_task(
        run_backfill, tenant_id, vendor, BACKFILL_PAGES_PER_INVOCATION
    )
    return {"status": "resumed", "vendor": vendor}
