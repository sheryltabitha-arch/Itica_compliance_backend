"""
app/routers/webhook.py

Routes:
  POST /api/webhook/ingest/{tenant_id}            — LEGACY, unchanged behavior.
                                                      Single secret from tenant_integrations.
                                                      Existing integrations keep working as-is.
  POST /api/webhook/ingest/{tenant_id}/{vendor}    — NEW. Per-vendor secret from
                                                      integration_connections, set up via
                                                      POST /api/integrations/connect.

Both paths share the same HMAC-verify + fan-out logic via _process_webhook().
Nothing about the legacy path's behavior changes — this is additive.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from app.middleware.auth import get_supabase
from app.services.retry import RetryExhausted, with_retry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhook", tags=["webhook"])


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _process_webhook(
    tenant_id: str,
    webhook_secret: str,
    raw_body: bytes,
    signature: str,
    source: str,
    vendor: str | None,
) -> dict:
    supabase = get_supabase()

    if not _verify_signature(webhook_secret, raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        import json
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type   = body.get("event_type",   "UNKNOWN")
    reference_id = body.get("reference_id", "")
    payload_data = body.get("payload",      {})

    event_id = str(uuid.uuid4())
    try:
        await with_retry(
            lambda: supabase.table("webhook_events").insert({
                "id":          event_id,
                "tenant_id":   tenant_id,
                "event_type":  event_type,
                "payload":     payload_data,
                "source":      source,
                "vendor":      vendor,  # null for legacy single-secret path
                "received_at": _now_utc(),
            }).execute(),
            label="webhook_events insert",
        )
    except RetryExhausted as e:
        # Nothing committed yet at this point — safe to 502 and let the
        # vendor's own webhook-delivery retry mechanism redeliver.
        raise HTTPException(status_code=502, detail=f"Could not record webhook event: {e}")

    try:
        if vendor:
            # New per-vendor path — update integration_connections, not tenant_integrations
            await with_retry(
                lambda: supabase.table("integration_connections").update({
                    "last_synced_at": _now_utc(),
                    "updated_at":     _now_utc(),
                }).eq("tenant_id", tenant_id).eq("vendor", vendor).execute(),
                label="integration_connections last_synced_at update",
            )
        else:
            # Legacy path — unchanged behavior
            await with_retry(
                lambda: supabase.table("tenant_integrations").upsert({
                    "tenant_id":         tenant_id,
                    "webhook_last_ping": _now_utc(),
                    "updated_at":        _now_utc(),
                }, on_conflict="tenant_id").execute(),
                label="tenant_integrations last_ping update",
            )
    except RetryExhausted as e:
        # Non-fatal — the event itself is already recorded above. Log and
        # continue; a stale last_synced_at timestamp is a cosmetic issue,
        # not a data-loss one.
        logger.warning(f"Could not update last-synced timestamp (non-fatal): {e}")

    prefix = event_type.lower().split(".")[0]

    try:
        if prefix == "kyc":
            await with_retry(
                lambda: supabase.table("kyc_documents").insert({
                    "tenant_id":     tenant_id,
                    "created_by":    "webhook",
                    "import_source": source,
                    "event_type":    event_type,
                    "reference_id":  reference_id,
                    "payload":       payload_data,
                    "source_event":  event_id,
                    "created_at":    _now_utc(),
                }).execute(),
                label="kyc_documents fan-out insert",
            )

        elif prefix == "aml":
            await with_retry(
                lambda: supabase.table("audit_events").insert({
                    "tenant_id":     tenant_id,
                    "created_by":    "webhook",
                    "import_source": source,
                    "event_type":    event_type,
                    "detail":        f"Webhook AML event | Ref: {reference_id}",
                    "reference_id":  reference_id,
                    "hash":          "",
                    "previous_hash": "",
                    "created_at":    _now_utc(),
                }).execute(),
                label="audit_events fan-out insert",
            )

        elif prefix == "decision":
            await with_retry(
                lambda: supabase.table("decisions").insert({
                    "tenant_id":      tenant_id,
                    "created_by":     "webhook",
                    "import_source":  source,
                    "decision_type":  event_type,
                    "reference_id":   reference_id,
                    "risk_tier":      payload_data.get("risk_tier", "Unknown"),
                    "rationale":      payload_data.get("rationale"),
                    "hash":           "",
                    "created_at":     _now_utc(),
                }).execute(),
                label="decisions fan-out insert",
            )
    except RetryExhausted as e:
        # The base webhook_events row is already safely recorded — the
        # fan-out can be replayed from it later (e.g. a reconciliation job
        # reading webhook_events where the matching fan-out row is missing).
        # We still surface this as a 502 so the vendor's delivery system
        # knows this attempt didn't fully succeed.
        logger.error(f"Fan-out write failed after retries for event {event_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Event recorded but fan-out failed: {e}")

    return {"status": "accepted", "event_id": event_id}


# ── legacy: single secret per tenant ─────────────────────────────────────────────

@router.post("/ingest/{tenant_id}")
async def webhook_ingest_legacy(tenant_id: str, request: Request):
    raw_body  = await request.body()
    signature = request.headers.get("X-Itica-Signature", "")
    source    = request.headers.get("X-Itica-Source",    "webhook")

    supabase = get_supabase()
    secret_result = (
        supabase.table("tenant_integrations")
        .select("webhook_secret")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    if not secret_result.data or not secret_result.data[0].get("webhook_secret"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found or webhook not configured")

    webhook_secret = secret_result.data[0]["webhook_secret"]
    return await _process_webhook(tenant_id, webhook_secret, raw_body, signature, source, vendor=None)


# ── new: per-vendor secret via integration_connections ───────────────────────────

@router.post("/ingest/{tenant_id}/{vendor}")
async def webhook_ingest_vendor(tenant_id: str, vendor: str, request: Request):
    raw_body  = await request.body()
    signature = request.headers.get("X-Itica-Signature", "")
    source    = request.headers.get("X-Itica-Source", vendor)

    supabase = get_supabase()
    conn_result = (
        supabase.table("integration_connections")
        .select("webhook_secret, active")
        .eq("tenant_id", tenant_id)
        .eq("vendor", vendor)
        .execute()
    )
    if not conn_result.data or not conn_result.data[0].get("webhook_secret"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active '{vendor}' connection for this tenant — connect it via POST /api/integrations/connect first",
        )
    if not conn_result.data[0].get("active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"'{vendor}' connection is inactive")

    webhook_secret = conn_result.data[0]["webhook_secret"]
    return await _process_webhook(tenant_id, webhook_secret, raw_body, signature, source, vendor=vendor)
