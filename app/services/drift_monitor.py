from __future__ import annotations
import logging
from datetime import datetime, timezone
logger = logging.getLogger(__name__)

class DriftMonitor:
    def __init__(self, db):
        self._db = db

    async def compute_report(self, model_version, tenant_id=None, window_hours=24, baseline_hours=168):
        return {"model_version": model_version, "tenant_id": tenant_id,
                "window_hours": window_hours, "total_inferences": 0,
                "alerts": [], "has_alerts": False,
                "computed_at": datetime.now(timezone.utc).isoformat()}
