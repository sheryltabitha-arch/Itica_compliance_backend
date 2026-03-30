from __future__ import annotations
import hashlib, logging, os, uuid
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import AuditActionType, UserRole
from app.services.audit_ledger import AuditLedger

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents", tags=["documents"])

SUPABASE_STORAGE_BUCKET = "kyc-documents"
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/tiff"}
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}


def _validate_file(file, content):
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(413, "File too large. Max 20MB.")
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, f"Unsupported content type '{content_type}'")
    ext = "." + (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file extension '{ext}'")


@router.post("/upload")
async def upload_document(
    file: Annotated[UploadFile, File()],
    current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer)),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    _validate_file(file, content)

    sha256 = hashlib.sha256(content).hexdigest()
    document_id = str(uuid.uuid5(
        uuid.UUID("00000000-0000-0000-0000-000000000000"),
        f"{current.tenant_id}:{sha256}"
    ))

    # Upload to Supabase Storage instead of S3
    storage_path = f"tenants/{current.tenant_id}/documents/{document_id}"
    try:
        sb = get_supabase()
        sb.storage.from_(SUPABASE_STORAGE_BUCKET).upload(
            path=storage_path,
            file=content,
            file_options={
                "content-type": file.content_type or "application/octet-stream",
                "x-upsert": "true",
            }
        )
    except Exception as e:
        logger.error(f"Supabase Storage upload failed: {e}")
        raise HTTPException(502, f"Storage upload failed: {str(e)}")

    # Record in kyc_documents table
    try:
        sb.table("kyc_documents").insert({
            "id": document_id,
            "tenant_id": str(current.tenant_id),
            "user_id": str(current.user_id),
            "original_filename": file.filename,
            "storage_path": storage_path,
            "sha256_hash": sha256,
            "size_bytes": len(content),
            "content_type": file.content_type,
            "status": "uploaded",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"kyc_documents table insert failed (non-fatal): {e}")

    # Audit log
    audit = AuditLedger(db)
    await audit.record(
        tenant_id=current.tenant_id,
        action_type=AuditActionType.document_uploaded,
        user_id=current.user_id,
        resource_type="document",
        resource_id=document_id,
        resource_hash=sha256,
        payload={"original_filename": file.filename, "size_bytes": len(content)},
    )
    await db.commit()

    return {
        "document_id": document_id,
        "sha256_hash": sha256,
        "storage_path": storage_path,
        "size_bytes": len(content),
        "filename": file.filename,
        "status": "uploaded",
    }


@router.get("/{document_id}")
async def get_document_metadata(
    document_id: str,
    current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer)),
):
    storage_path = f"tenants/{current.tenant_id}/documents/{document_id}"
    try:
        sb = get_supabase()
        result = sb.table("kyc_documents").select("*").eq("id", document_id).eq("tenant_id", str(current.tenant_id)).execute()
        if result.data:
            return result.data[0]
        files = sb.storage.from_(SUPABASE_STORAGE_BUCKET).list(f"tenants/{current.tenant_id}/documents")
        match = next((f for f in (files or []) if f["name"] == document_id), None)
        if match:
            return {"document_id": document_id, "storage_path": storage_path, "size_bytes": match.get("metadata", {}).get("size")}
        raise HTTPException(404, f"Document {document_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Storage lookup failed: {str(e)}")
