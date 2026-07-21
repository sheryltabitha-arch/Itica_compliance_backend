"""
app/services/integrations/base.py

Vendor-agnostic integration connector contract.

Design goals (per requirements):
  1. Works for ANY tool — Fireblocks, Sardine, Unit21, an internal client
     tool, anything — by implementing this same interface.
  2. Bi-directional: inbound (fetch_cases/backfill) AND outbound (push_decision).
  3. Full-history backfill is RESUMABLE. A vendor's full history could be
     years of data across thousands of paginated API calls — this must
     survive a crash/restart/deploy without re-pulling everything.

A connector class does NOT talk to Supabase directly. It only knows how to
talk to the vendor's API and normalize the vendor's data shape into Itica's
internal event shape. Persistence/orchestration lives in the sync service
that calls these methods (kept separate so connectors stay easy to test).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SyncDirection(str, Enum):
    inbound = "inbound"
    outbound = "outbound"
    bidirectional = "bidirectional"


class BackfillStatus(str, Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


@dataclass
class BackfillPage:
    """
    One page of results from a vendor's history API.

    cursor: opaque token to resume from. Connector defines its own format
            (could be a vendor page token, a timestamp, an offset — whatever
            the vendor's API uses). The sync service persists this verbatim
            and passes it back unchanged on the next call.
    has_more: False means this connector believes history is exhausted.
    events: normalized events, ready to write to Itica's tables.
    """
    events: list[dict]
    cursor: str | None
    has_more: bool


@dataclass
class ConnectorEvent:
    """Normalized shape every connector must produce, regardless of vendor.

    actor/rationale are explicit, first-class fields — not buried in
    metadata — so every vendor connector is forced to either populate them
    or explicitly leave them None. None is a real, honest answer (it means
    "this vendor doesn't structurally capture who/why") and the frontend
    should render it as "not provided by vendor" rather than leaving the
    field blank or inferring a value.
    """
    external_id: str          # vendor's own ID for this event/case — used for dedup
    event_type: str           # mapped to Itica's kyc.*/aml.*/decision.* convention
    occurred_at: datetime     # when the event happened at the VENDOR, not when we synced it
    reference_id: str | None
    risk_tier: str | None
    raw_payload: dict
    actor: str | None = None       # who acted at the vendor (analyst id/name/email) — None if vendor doesn't expose this
    rationale: str | None = None   # why, in the vendor's own words/reason-code — None if vendor doesn't expose this
    metadata: dict = field(default_factory=dict)


class IntegrationConnector(ABC):
    """
    Base contract every vendor integration must implement.

    Subclasses register themselves via the @register_connector decorator
    in registry.py — the router/sync-service never imports a vendor class
    directly, it looks it up by name. That's what makes adding a new vendor
    later a self-contained change.
    """

    vendor_name: str
    supports_outbound: bool = False  # override True once push_decision is real

    def __init__(self, tenant_id: str, credentials: dict[str, Any]):
        self.tenant_id = tenant_id
        self.credentials = credentials

    # ── inbound ──────────────────────────────────────────────────────────

    @abstractmethod
    async def authenticate(self) -> bool:
        """
        Lightweight credential check (e.g. a /ping or /me call).
        Must NOT pull data — this is called on connection setup to fail
        fast with a clear error before any sync work starts.
        """
        ...

    @abstractmethod
    async def backfill_page(self, cursor: str | None) -> BackfillPage:
        """
        Fetch ONE page of full historical data.

        cursor=None means "start from the beginning of vendor history."
        The sync service loops this until has_more is False, persisting
        `cursor` after every page so a crash mid-backfill resumes exactly
        where it left off rather than restarting.
        """
        ...

    @abstractmethod
    async def fetch_incremental(self, since: datetime) -> list[ConnectorEvent]:
        """
        Live/ongoing sync — events since the last successful sync.
        Used after backfill_page has completed (has_more=False) and the
        connection moves into steady-state polling, if the vendor doesn't
        support a push webhook.
        """
        ...

    # ── outbound ─────────────────────────────────────────────────────────

    async def push_decision(self, decision_id: str, payload: dict) -> bool:
        """
        Outbound: send an Itica decision/case-update back to the vendor.
        Default no-op — override and set supports_outbound=True for vendors
        that accept writes (many compliance vendors are read-only from
        Itica's perspective, which is fine).
        """
        raise NotImplementedError(
            f"{self.vendor_name} connector does not support outbound sync"
        )

    # ── shared normalization hook ───────────────────────────────────────

    @abstractmethod
    def normalize(self, raw_event: dict) -> ConnectorEvent:
        """Map one vendor-native record into Itica's internal ConnectorEvent shape."""
        ...
