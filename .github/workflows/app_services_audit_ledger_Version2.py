"""
Itica — Audit Ledger Service
Immutable append-only audit log for compliance.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AuditEvent, AuditActionType

logger = logging.getLogger(__name__)


class AuditLedger:
    """Immutable audit log service. All operations are append-only."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def record(
        self,
        tenant_id: str,
        action_type: AuditActionType,
        user_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        resource_hash: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """
        Record an audit event.
        Returns the event ID.
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        event = AuditEvent(
            id=event_id,
            tenant_id=tenant_id,
            action_type=action_type.value,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_hash=resource_hash,
            payload=payload or {},
            created_at=now,
        )

        self._db.add(event)
        logger.debug(
            "Audit event recorded: action=%s resource=%s/%s user=%s",
            action_type.value, resource_type, resource_id, user_id,
        )

        return event_id