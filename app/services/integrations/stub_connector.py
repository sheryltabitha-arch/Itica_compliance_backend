"""
app/services/integrations/stub_connector.py

Generic placeholder connector — registered under "manual" / "generic".

You don't have Fireblocks (or any other specific vendor's) credentials yet,
but clients will name new tools before you have time to build a bespoke
connector for each one. This lets a tenant "connect" any vendor today via
the existing manual CSV/JSON import flow (integrations.py's /import route)
while a real API-backed connector is written later — same registry slot,
same downstream tables, zero special-casing in the router.

Swapping this for a real connector later is a one-file change: write
e.g. fireblocks.py implementing the same ABC, register it under
"fireblocks" instead of routing that vendor through this stub.
"""
from __future__ import annotations

from datetime import datetime

from app.services.integrations.base import (
    BackfillPage,
    ConnectorEvent,
    IntegrationConnector,
)
from app.services.integrations.registry import register_connector


@register_connector("manual")
class ManualImportConnector(IntegrationConnector):
    """
    No live API calls. authenticate() always succeeds (there's nothing to
    check). backfill_page()/fetch_incremental() return empty — actual data
    arrives via the existing /api/integrations/import file-upload endpoint,
    which writes straight to audit_events. This connector exists so every
    tenant_integrations row can have a consistent `vendor` value and the
    sync orchestration code doesn't need an "or maybe there's no connector
    at all" branch.
    """

    supports_outbound = False

    async def authenticate(self) -> bool:
        return True

    async def backfill_page(self, cursor: str | None) -> BackfillPage:
        return BackfillPage(events=[], cursor=None, has_more=False)

    async def fetch_incremental(self, since: datetime) -> list[ConnectorEvent]:
        return []

    def normalize(self, raw_event: dict) -> ConnectorEvent:
        # Reuses the same field-mapping convention as integrations.py's
        # map_row() for unit21/sardine — kept here so a future real
        # connector for the same vendor can lift this mapping directly.
        #
        # actor/rationale are explicitly None here — manual CSV/JSON import
        # has no vendor API to pull these from. A real connector should
        # replace these None values with whatever the vendor's payload
        # actually contains (see base.py's ConnectorEvent docstring).
        return ConnectorEvent(
            external_id=str(raw_event.get("id", "")),
            event_type=raw_event.get("event_type", "manual.imported"),
            occurred_at=raw_event.get("occurred_at") or datetime.utcnow(),
            reference_id=raw_event.get("reference_id"),
            risk_tier=raw_event.get("risk_tier"),
            raw_payload=raw_event,
            actor=None,
            rationale=None,
        )
