from __future__ import annotations
import logging, uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models import AuditEvent

logger = logging.getLogger(__name__)

class AuditLedger:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def record(self, tenant_id, action_type, user_id=None,
                     resource_type=None, resource_id=None, resource_hash=None, payload=None) -> str:
        event_id = str(uuid.uuid4())
        action = action_type.value if hasattr(action_type, "value") else str(action_type)
        event = AuditEvent(
            id=event_id, tenant_id=tenant_id, action_type=action,
            user_id=user_id, resource_type=resource_type, resource_id=resource_id,
            resource_hash=resource_hash, payload=payload or {},
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(event)
        return event_id
