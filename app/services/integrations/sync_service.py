"""
app/services/integrations/sync_service.py  (v2 — retry + encryption + honest push_decision)

Changes from v1:
  - All connector calls (authenticate, backfill_page, fetch_incremental,
    push_decision) now go through with_retry() instead of bare awaits.
  - Credentials are decrypted via crypto.py before being handed to a
    connector, since integrations.py now stores them encrypted.
  - push_decision() is a new orchestration function: if the connector
    doesn't support outbound, or a real send fails after retries, the
    decision is queued in integration_outbound_queue with an honest status
    rather than raising NotImplementedError up to the caller.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.middleware.auth import get_supabase
from app.services.integrations.base import ConnectorEvent
from app.services.integrations.crypto import decrypt_credentials
from app.services.integrations.registry import get_connector_class
from app.services.retry import RetryExhausted, with_retry

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_connection(supabase, tenant_id: str, vendor: str) -> dict:
    result = (
        supabase.table("integration_connections")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("vendor", vendor)
        .execute()
    )
    if not result.data:
        raise ValueError(f"No integration_connections row for tenant={tenant_id} vendor={vendor}")
    return result.data[0]


def _build_connector(conn: dict, tenant_id: str, vendor: str):
    connector_cls = get_connector_class(vendor)
    encrypted = conn.get("credentials")
    credentials = decrypt_credentials(encrypted) if isinstance(encrypted, str) else (encrypted or {})
    return connector_cls(tenant_id, credentials)


def _persist_events(supabase, tenant_id: str, vendor: str, events: list[ConnectorEvent]) -> int:
    if not events:
        return 0
    rows = [
        {
            "tenant_id":    tenant_id,
            "vendor":       vendor,
            "external_id":  e.external_id,
            "event_type":   e.event_type,
            "reference_id": e.reference_id,
            "risk_tier":    e.risk_tier,
            "occurred_at":  e.occurred_at.isoformat() if isinstance(e.occurred_at, datetime) else e.occurred_at,
            "raw_payload":  e.raw_payload,
        }
        for e in events
    ]
    supabase.table("integration_synced_events").upsert(
        rows, on_conflict="tenant_id,vendor,external_id"
    ).execute()
    return len(rows)


# ── backfill ─────────────────────────────────────────────────────────────────

async def run_backfill(tenant_id: str, vendor: str, max_pages: int | None = None) -> dict:
    supabase = get_supabase()
    conn = await _get_connection(supabase, tenant_id, vendor)
    connector = _build_connector(conn, tenant_id, vendor)

    if conn["backfill_status"] == "completed":
        return {"status": "already_completed", "vendor": vendor}

    supabase.table("integration_connections").update({
        "backfill_status": "in_progress",
        "backfill_started_at": conn.get("backfill_started_at") or _now_utc(),
        "updated_at": _now_utc(),
    }).eq("id", conn["id"]).execute()

    cursor = conn.get("backfill_cursor")
    pages_processed = 0
    total_events = 0

    try:
        while True:
            page = await with_retry(
                lambda: connector.backfill_page(cursor),
                label=f"{vendor} backfill_page",
            )
            total_events += _persist_events(supabase, tenant_id, vendor, page.events)
            cursor = page.cursor
            pages_processed += 1

            supabase.table("integration_connections").update({
                "backfill_cursor": cursor,
                "updated_at": _now_utc(),
            }).eq("id", conn["id"]).execute()

            if not page.has_more:
                supabase.table("integration_connections").update({
                    "backfill_status": "completed",
                    "backfill_completed_at": _now_utc(),
                    "updated_at": _now_utc(),
                }).eq("id", conn["id"]).execute()
                return {
                    "status": "completed", "vendor": vendor,
                    "pages_processed": pages_processed, "events_synced": total_events,
                }

            if max_pages is not None and pages_processed >= max_pages:
                return {
                    "status": "in_progress", "vendor": vendor,
                    "pages_processed": pages_processed, "events_synced": total_events,
                    "resume_cursor": cursor,
                }

    except RetryExhausted as e:
        logger.error(f"Backfill failed (retries exhausted) tenant={tenant_id} vendor={vendor}: {e}")
        supabase.table("integration_connections").update({
            "backfill_status": "failed",
            "backfill_error": str(e),
            "updated_at": _now_utc(),
        }).eq("id", conn["id"]).execute()
        raise


# ── incremental sync ─────────────────────────────────────────────────────────

async def run_incremental_sync(tenant_id: str, vendor: str) -> dict:
    supabase = get_supabase()
    conn = await _get_connection(supabase, tenant_id, vendor)
    connector = _build_connector(conn, tenant_id, vendor)

    since_str = conn.get("last_synced_at")
    since = (
        datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        if since_str else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )

    try:
        events = await with_retry(
            lambda: connector.fetch_incremental(since),
            label=f"{vendor} fetch_incremental",
        )
        count = _persist_events(supabase, tenant_id, vendor, events)
        supabase.table("integration_connections").update({
            "last_synced_at": _now_utc(),
            "last_sync_error": None,
            "updated_at": _now_utc(),
        }).eq("id", conn["id"]).execute()
        return {"status": "synced", "vendor": vendor, "events_synced": count}
    except RetryExhausted as e:
        logger.error(f"Incremental sync failed tenant={tenant_id} vendor={vendor}: {e}")
        supabase.table("integration_connections").update({
            "last_sync_error": str(e),
            "updated_at": _now_utc(),
        }).eq("id", conn["id"]).execute()
        raise


# ── outbound (push_decision) — the "bidirectional" half ────────────────────────

async def push_decision(tenant_id: str, vendor: str, decision_id: str, payload: dict) -> dict:
    """
    Single entry point for sending an Itica decision back to a vendor.
    Callers (e.g. decisions.py) should call THIS, never a connector's
    push_decision() directly — this function guarantees a result either way:

      - connector doesn't support outbound      -> queued, status='unsupported'
      - connector supports it, send succeeds     -> status='sent'
      - connector supports it, retries exhausted -> queued, status='failed'

    Nothing here raises NotImplementedError up to the caller. A decision is
    either confirmed sent, or sitting in integration_outbound_queue for
    manual follow-up/retry — never silently lost, never a crash.
    """
    supabase = get_supabase()
    conn = await _get_connection(supabase, tenant_id, vendor)
    connector = _build_connector(conn, tenant_id, vendor)

    queue_row = {
        "tenant_id":   tenant_id,
        "vendor":      vendor,
        "decision_id": decision_id,
        "payload":     payload,
    }

    if not connector.supports_outbound:
        queue_row["status"] = "unsupported"
        supabase.table("integration_outbound_queue").insert(queue_row).execute()
        logger.info(f"{vendor} connector has no outbound support — queued decision {decision_id} for manual handling")
        return {"status": "unsupported", "vendor": vendor, "decision_id": decision_id}

    try:
        await with_retry(
            lambda: connector.push_decision(decision_id, payload),
            label=f"{vendor} push_decision",
        )
        queue_row["status"] = "sent"
        queue_row["sent_at"] = _now_utc()
        supabase.table("integration_outbound_queue").insert(queue_row).execute()
        return {"status": "sent", "vendor": vendor, "decision_id": decision_id}
    except RetryExhausted as e:
        queue_row["status"] = "failed"
        queue_row["last_error"] = str(e)
        supabase.table("integration_outbound_queue").insert(queue_row).execute()
        logger.error(f"Push to {vendor} failed after retries for decision {decision_id}: {e}")
        return {"status": "failed", "vendor": vendor, "decision_id": decision_id, "error": str(e)}
