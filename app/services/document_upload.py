from __future__ import annotations
import hashlib, logging, os, uuid
from datetime import datetime, timezone
from typing import Annotated
import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.middleware.auth import CurrentUser, require_min_role
from app.models.models import AuditActionType, UserRole
from app.services.audit_ledger import AuditLedger

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents", tags=["documents"])
S3_BUCKET = os.environ.get("S3_BUCKET_DOCUMENTS", "itica-documents-encrypted")
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/tiff"}
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}

def _s3_client():
    return boto3.client("s3")

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
async def upload_document(file: Annotated[UploadFile, File()],
                          current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer)),
                          db: AsyncSession = Depends(get_db)):
    content = await file.read()
    _validate_file(file, content)
    sha256 = hashlib.sha256(content).hexdigest()
    document_id = str(uuid.uuid5(uuid.UUID("00000000-0000-0000-0000-000000000000"),
                                  f"{current.tenant_id}:{sha256}"))
    s3_key = f"tenants/{current.tenant_id}/documents/{document_id}"
    try:
        _s3_client().put_object(Bucket=S3_BUCKET, Key=s3_key, Body=content,
                                ContentType=file.content_type or "application/octet-stream",
                                Metadata={"tenant_id": current.tenant_id, "sha256": sha256},
                                ServerSideEncryption="AES256")
    except ClientError as e:
        raise HTTPException(502, f"Storage upload failed: {e.response['Error']['Message']}")
    audit = AuditLedger(db)
    await audit.record(tenant_id=current.tenant_id, action_type=AuditActionType.document_uploaded,
                       user_id=current.user_id, resource_type="document",
                       resource_id=document_id, resource_hash=sha256,
                       payload={"original_filename": file.filename, "size_bytes": len(content)})
    await db.commit()
    return {"document_id": document_id, "sha256_hash": sha256, "s3_key": s3_key,
            "size_bytes": len(content), "filename": file.filename, "status": "uploaded"}

@router.get("/{document_id}")
async def get_document_metadata(document_id: str,
                                current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer))):
    s3_key = f"tenants/{current.tenant_id}/documents/{document_id}"
    try:
        head = _s3_client().head_object(Bucket=S3_BUCKET, Key=s3_key)
        return {"document_id": document_id, "s3_key": s3_key,
                "size_bytes": head["ContentLength"], "last_modified": head["LastModified"].isoformat()}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            raise HTTPException(404, f"Document {document_id} not found")
        raise HTTPException(502, f"Storage lookup failed: {e.response['Error']['Message']}")
