from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

class HumanReviewService:
    def __init__(self, db, audit):
        self.db = db
        self.audit = audit

    async def list_pending_tasks(self, tenant_id, limit=50, offset=0):
        return []

    async def get_task_detail(self, task_id, tenant_id):
        return None

    async def submit_correction(self, review_task_id, reviewer_id, tenant_id, corrections, ip_address=None):
        return f"corr-{review_task_id}"
