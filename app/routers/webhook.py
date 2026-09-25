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
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from app.middleware.auth import get_supabase
from app.services.audit_hash import compute_event_hash
from app.services.retry import RetryExhausted, with_retry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhook", tags=["webhook"])


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _insert_once(supabase, table: str, row: dict, *, label: str) -> dict:
    """
    A plain (non-upsert) insert is not safe to retry as-is: if the server
    actually wrote the row but the ack was lost (a transient network
    blip), with_retry re-runs the same insert and — since these tables use
    server-generated ids — that produces a second, duplicate row instead
    of detecting "this already happened".

    Assigning the id client-side before the first attempt fixes that: a
    retry sends the identical row, including the identical id, so a
    genuine duplicate now hits that table's own primary-key constraint
    instead of silently succeeding as a new row. We treat that specific
    failure as "already inserted" and return the existing row.
    """
    row = dict(row)
    row.setdefault("id", str(uuid.uuid4()))
    try:
        result = await with_retry(lambda: supabase.table(table).insert(row).execute(), label=label)
        return result.data[0]
    except RetryExhausted as e:
        underlying = str(getattr(e, "last_error", e))
        if "duplicate key" in underlying.lower() or "23505" in underlying:
            existing = supabase.table(table).select("*").eq("id", row["id"]).limit(1).execute()
            if existing.data:
                logger.info(f"{label}: retry collided with its own earlier (acked-lost) insert — using existing row")
                return existing.data[0]
        raise


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
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type   = body.get("event_type",   "UNKNOWN")
    reference_id = body.get("reference_id", "")
    payload_data = body.get("payload",      {})

    # Some vendors redeliver a logically-identical event with different
    # bytes (a fresh timestamp, reordered JSON), which body_hash alone
    # won't catch. If the vendor includes its own stable id anywhere
    # sensible, prefer that for dedup; body_hash remains the fallback for
    # vendors that don't send one.
    vendor_event_id = body.get("event_id") or body.get("id") or payload_data.get("id")

    body_hash = hashlib.sha256(raw_body).hexdigest()

    if vendor_event_id:
        existing_by_vendor_id = (
            supabase.table("webhook_events")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("vendor", vendor)
            .eq("vendor_event_id", vendor_event_id)
            .limit(1)
            .execute()
        )
        if existing_by_vendor_id.data:
            logger.info(
                f"Duplicate webhook delivery detected via vendor_event_id "
                f"(tenant {tenant_id}, vendor {vendor}, id {vendor_event_id}) — skipping"
            )
            return {"status": "duplicate", "event_id": existing_by_vendor_id.data[0]["id"]}

    # Idempotency check: most webhook redelivery systems resend the exact
    # same payload bytes on retry. Hashing the raw body gives a reliable
    # dedup key without depending on any vendor providing their own event
    # ID. This runs BEFORE any fan-out logic, so a redelivered webhook
    # can't create duplicate kyc_documents/audit_events/decisions rows —
    # not just customers/transactions, which already had their own upsert
    # protection.
    existing = (
        supabase.table("webhook_events")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("body_hash", body_hash)
        .limit(1)
        .execute()
    )
    if existing.data:
        logger.info(
            f"Duplicate webhook delivery detected (tenant {tenant_id}, "
            f"body_hash {body_hash[:12]}...) — skipping reprocessing"
        )
        return {"status": "duplicate", "event_id": existing.data[0]["id"]}

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
                "body_hash":   body_hash,
                "vendor_event_id": vendor_event_id,
                "received_at": _now_utc(),
            }).execute(),
            label="webhook_events insert",
        )
    except RetryExhausted as e:
        # The pre-check above has a gap: two identical deliveries can both
        # pass it before either has inserted. The unique index on
        # (tenant_id, body_hash) catches that at the DB level, but that's a
        # permanent violation, not a transient one — retrying it 4 times
        # with backoff (as the default `retryable=(Exception,)` does) just
        # wastes several seconds before failing the same way every time.
        # "duplicate key" / error code 23505 is Postgres's unique-violation
        # signal; check for it in the wrapped error's text since we don't
        # have the exact exception class this Supabase client raises.
        underlying = str(getattr(e, "last_error", e))
        if "duplicate key" in underlying.lower() or "23505" in underlying:
            lookup = supabase.table("webhook_events").select("id").eq("tenant_id", tenant_id)
            lookup = (
                lookup.eq("vendor", vendor).eq("vendor_event_id", vendor_event_id)
                if vendor_event_id else lookup.eq("body_hash", body_hash)
            )
            existing = lookup.limit(1).execute()
            if existing.data:
                logger.info(
                    f"Concurrent duplicate webhook delivery (tenant {tenant_id}, "
                    f"body_hash {body_hash[:12]}...) — race lost, treating as duplicate"
                )
                return {"status": "duplicate", "event_id": existing.data[0]["id"]}
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
            await _insert_once(
                supabase, "kyc_documents",
                {
                    "tenant_id":     tenant_id,
                    "created_by":    "webhook",
                    "import_source": source,
                    "event_type":    event_type,
                    "reference_id":  reference_id,
                    "payload":       payload_data,
                    "source_event":  event_id,
                    "created_at":    _now_utc(),
                },
                label="kyc_documents fan-out insert",
            )

        elif prefix == "aml":
            prev = (
                supabase.table("audit_events")
                .select("hash")
                .eq("tenant_id", tenant_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
            aml_detail = f"Webhook AML event | Ref: {reference_id}"
            aml_created_at = _now_utc()
            event_hash = compute_event_hash(
                tenant_id, event_type, aml_detail, previous_hash, aml_created_at
            )
            await _insert_once(
                supabase, "audit_events",
                {
                    "tenant_id":     tenant_id,
                    "created_by":    "webhook",
                    "import_source": source,
                    "event_type":    event_type,
                    "detail":        aml_detail,
                    "reference_id":  reference_id,
                    "hash":          event_hash,
                    "previous_hash": previous_hash,
                    "created_at":    aml_created_at,
                    "subject_id":    reference_id or None,
                },
                label="audit_events fan-out insert",
            )

            # Item 50: an incoming AML alert should actually create a case,
            # not just log an audit event — otherwise "alert ingestion ->
            # case creation -> queue & investigation" is a claim with no
            # real pipeline behind it. find-or-create by case_reference so
            # a second alert for the same reference_id doesn't spawn a
            # duplicate case; it lands as a new audit event on the existing
            # one instead.
            if reference_id:
                case_reference = f"CASE-{reference_id}"
                existing_case = (
                    supabase.table("customer_cases").select("id")
                    .eq("tenant_id", tenant_id).eq("case_reference", case_reference)
                    .limit(1).execute()
                )
                if not existing_case.data:
                    await _insert_once(
                        supabase, "customer_cases",
                        {
                            "tenant_id":      tenant_id,
                            "case_reference": case_reference,
                            "customer_id":    reference_id,
                            "case_type":      "suspicious_transaction",
                            "status":         "alert_received",
                            "notes":          f"Auto-opened from webhook AML alert (event_type: {event_type})",
                            "created_by":     None,
                            "decision_ids":   [],
                            "kyc_document_ids": [],
                        },
                        label="customer_cases auto-create from AML alert",
                    )

        elif prefix == "decision":
            decision_hash = hashlib.sha256(
                f"{tenant_id}|{event_type}|{reference_id}|webhook|{event_id}".encode()
            ).hexdigest()
            new_decision = await _insert_once(
                supabase, "decisions",
                {
                    "tenant_id":      tenant_id,
                    "created_by":     "webhook",
                    "import_source":  source,
                    "decision_type":  event_type,
                    "reference_id":   reference_id,
                    "risk_tier":      payload_data.get("risk_tier", "Unknown"),
                    "rationale":      payload_data.get("rationale"),
                    "hash":           decision_hash,
                    "created_at":     _now_utc(),
                },
                label="decisions fan-out insert",
            )
            # Every other path that creates a decision (decisions.py) also
            # writes an audit_events row for it. Without this, a
            # webhook-originated decision is invisible to
            # GET /api/v1/customers/timeline, which finds decisions purely
            # through audit_events.subject_id.
            prev = (
                supabase.table("audit_events")
                .select("hash")
                .eq("tenant_id", tenant_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
            decision_detail = f"Webhook decision | Type: {event_type} | Ref: {reference_id}"
            decision_event_hash = compute_event_hash(
                tenant_id, "DECISION_CREATED", decision_detail, previous_hash, new_decision["created_at"]
            )
            await _insert_once(
                supabase, "audit_events",
                {
                    "tenant_id":     tenant_id,
                    "created_by":    "webhook",
                    "import_source": source,
                    "event_type":    "DECISION_CREATED",
                    "detail":        decision_detail,
                    "hash":          decision_event_hash,
                    "previous_hash": previous_hash,
                    "created_at":    new_decision["created_at"],
                    "subject_id":    str(new_decision["id"]),
                },
                label="audit_events decision fan-out insert",
            )

        elif prefix == "customer":
            # Phase B: customer profile sync from a client's own systems.
            # Field names below are best-effort common conventions (id,
            # name, country, etc. as fallbacks) since no specific vendor's
            # schema is confirmed yet — narrow these once a real vendor's
            # actual field names are known.
            customer_id = payload_data.get("customer_id") or payload_data.get("id") or reference_id
            if not customer_id:
                logger.warning(f"customer.* webhook event {event_id} had no customer_id/reference_id — skipped fan-out")
            else:
                await with_retry(
                    lambda: supabase.table("customers").upsert({
                        "tenant_id":            tenant_id,
                        "customer_id":          customer_id,
                        "full_name":            payload_data.get("full_name") or payload_data.get("name"),
                        "entity_type":          payload_data.get("entity_type"),
                        "jurisdiction":         payload_data.get("jurisdiction") or payload_data.get("country"),
                        "risk_rating":          payload_data.get("risk_rating"),
                        "ubo_verified":         payload_data.get("ubo_verified"),
                        "kyc_review_date":      payload_data.get("kyc_review_date"),
                        "pep_screening":        payload_data.get("pep_screening"),
                        "sanctions_screening":  payload_data.get("sanctions_screening"),
                        "source_system":        source,
                        "external_id":          payload_data.get("id") or customer_id,
                        "updated_at":           _now_utc(),
                    }, on_conflict="tenant_id,customer_id").execute(),
                    label="customers fan-out upsert",
                )

        elif prefix == "transaction":
            # Upsert on (tenant_id, external_id) so a redelivered webhook
            # (no vendor event-id dedup exists yet) updates the same row
            # instead of creating a duplicate transaction.
            external_id = payload_data.get("id") or event_id
            await with_retry(
                lambda: supabase.table("transactions").upsert({
                    "tenant_id":     tenant_id,
                    "customer_id":   payload_data.get("customer_id") or reference_id,
                    "external_id":   external_id,
                    "source_system": source,
                    "occurred_at":   payload_data.get("occurred_at") or _now_utc(),
                    "amount":        payload_data.get("amount"),
                    "currency":      payload_data.get("currency"),
                    "counterparty":  payload_data.get("counterparty"),
                    "direction":     payload_data.get("direction"),
                    "flag":          payload_data.get("flag"),
                    "risk_score":    payload_data.get("risk_score"),
                    "raw_payload":   payload_data,
                }, on_conflict="tenant_id,external_id").execute(),
                label="transactions fan-out upsert",
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
