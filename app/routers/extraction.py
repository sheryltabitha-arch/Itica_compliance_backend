from __future__ import annotations
import logging, uuid
from datetime import datetime, timezone
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.middleware.auth import CurrentUser, require_min_role
from app.models.models import UserRole
from app.services.audit_ledger import AuditLedger

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/extraction", tags=["extraction"])

class ExtractRequest(BaseModel):
    document_id: str; model_version: str = "2.0.0"
    expected_weights_hash: Optional[str] = None
    country_hint: Optional[str] = None; min_age: int = 18

class ExtractResponse(BaseModel):
    extraction_id: str; document_id: str; status: str; model_version: str; created_at: str

@router.post("/extract", response_model=ExtractResponse)
async def extract_document(request: ExtractRequest,
                           current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
                           db: AsyncSession = Depends(get_db)):
    extraction_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    audit = AuditLedger(db)
    await audit.record(tenant_id=current.tenant_id, action_type="extraction_requested",
                       user_id=current.user_id, resource_type="extraction",
                       resource_id=extraction_id, payload={"document_id": request.document_id})
    await db.commit()
    return ExtractResponse(extraction_id=extraction_id, document_id=request.document_id,
                           status="processing", model_version=request.model_version,
                           created_at=now.isoformat())

@router.get("/extract/{extraction_id}")
async def get_extraction_result(extraction_id: str,
                                current: Annotated[CurrentUser, Depends(require_min_role(UserRole.compliance_officer))],
                                db: AsyncSession = Depends(get_db)):
    raise HTTPException(404, f"Extraction {extraction_id} not found")
