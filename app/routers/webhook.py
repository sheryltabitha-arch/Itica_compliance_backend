"""
app/routers/webhook.py

Routes:
  POST /api/webhook/ingest/{tenant_id}  — public, HMAC-verified ingest endpoint

Reads X-Itica-Signature (hex HMAC-SHA256 of raw body with tenant webhook_secret).
Reads X-Itica-Source for import_source attribution.
Fans out to secondary tables based on event_type prefix:
  kyc.*      → kyc_documents
  aml.*      → audit_events
  decision.* → decisions
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from app.middleware.auth import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhook", tags=["webhook"])


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/ingest/{tenant_id}")
async def webhook_ingest(tenant_id: str, request: Request):
    supabase = get_supabase()

    # ── read raw body before JSON parsing ─────────────────────────────────────
    raw_body  = await request.body()
    signature = request.headers.get("X-Itica-Signature", "")
    source    = request.headers.get("X-Itica-Source",    "webhook")

    # ── look up webhook secret ────────────────────────────────────────────────
    secret_result = (
        supabase.table("tenant_integrations")
        .select("webhook_secret")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    if not secret_result.data or not secret_result.data[0].get("webhook_secret"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found or webhook not configured")

    webhook_secret = secret_result.data[0]["webhook_secret"]

    # ── verify HMAC ───────────────────────────────────────────────────────────
    expected = hmac.new(
        webhook_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    # ── parse body ────────────────────────────────────────────────────────────
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type   = body.get("event_type",   "UNKNOWN")
    reference_id = body.get("reference_id", "")
    payload_data = body.get("payload",      {})

    # ── insert into webhook_events ────────────────────────────────────────────
    event_id = str(uuid.uuid4())
    supabase.table("webhook_events").insert({
        "id":          event_id,
        "tenant_id":   tenant_id,
        "event_type":  event_type,
        "payload":     payload_data,
        "source":      source,
        "received_at": _now_utc(),
    }).execute()

    # ── update last ping ──────────────────────────────────────────────────────
    supabase.table("tenant_integrations").upsert({
        "tenant_id":        tenant_id,
        "webhook_last_ping": _now_utc(),
        "updated_at":       _now_utc(),
    }, on_conflict="tenant_id").execute()

    # ── fan-out to secondary tables ───────────────────────────────────────────
    prefix = event_type.lower().split(".")[0]

    if prefix == "kyc":
        supabase.table("kyc_documents").insert({
            "tenant_id":     tenant_id,
            "created_by":    "webhook",
            "import_source": source,
            "event_type":    event_type,
            "reference_id":  reference_id,
            "payload":       payload_data,
            "source_event":  event_id,
            "created_at":    _now_utc(),
        }).execute()

    elif prefix == "aml":
        supabase.table("audit_events").insert({
            "tenant_id":     tenant_id,
            "created_by":    "webhook",
            "import_source": source,
            "event_type":    event_type,
            "detail":        f"Webhook AML event | Ref: {reference_id}",
            "reference_id":  reference_id,
            "hash":          "",
            "previous_hash": "",
            "created_at":    _now_utc(),
        }).execute()

    elif prefix == "decision":
        supabase.table("decisions").insert({
            "tenant_id":      tenant_id,
            "created_by":     "webhook",
            "import_source":  source,
            "decision_type":  event_type,
            "reference_id":   reference_id,
            "risk_tier":      payload_data.get("risk_tier", "Unknown"),
            "rationale":      payload_data.get("rationale"),
            "hash":           "",
            "created_at":     _now_utc(),
        }).execute()

    return {"status": "accepted", "event_id": event_id}
