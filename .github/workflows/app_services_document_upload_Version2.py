"""
Itica — Document Upload Service
Handles file upload, validation, and S3 storage.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
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

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".docx"}


def _s3_client():
    return boto3.client("s3")


def _validate_file(file: UploadFile, content: bytes) -> None:
    """Raise HTTPException if file fails any validation check."""
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            413,
            f"File exceeds maximum size of {MAX_FILE_SIZE_BYTES // (1024*1024)}MB. "
            f"Received {len(content) // (1024*1024)}MB."
        )

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            415,
            f"Unsupported content type '{content_type}'. "
            f"Allowed: {sorted(ALLOWED_CONTENT_TYPES)}"
        )

    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            415,
            f"Unsupported file extension '{ext}'. "
            f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )


@router.post("/upload")
async def upload_document(
    file: Annotated[UploadFile, File(description="KYC document")],
    current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer)),
    db: AsyncSession = Depends(get_db),
):
    """Upload a KYC document to S3."""
    content = await file.read()

    # Validate
    _validate_file(file, content)

    # Compute SHA-256
    sha256 = hashlib.sha256(content).hexdigest()

    # Generate document_id
    document_id = str(uuid.uuid5(
        uuid.UUID("00000000-0000-0000-0000-000000000000"),
        f"{current.tenant_id}:{sha256}"
    ))

    s3_key = f"tenants/{current.tenant_id}/documents/{document_id}"

    # Upload to S3
    try:
        s3 = _s3_client()
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=content,
            ContentType=file.content_type or "application/octet-stream",
            Metadata={
                "tenant_id": current.tenant_id,
                "original_name": file.filename or "unknown",
                "sha256": sha256,
                "uploaded_by": str(current.user_id),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            },
            ServerSideEncryption="AES256",
        )
    except ClientError as e:
        logger.exception("S3 upload failed for tenant=%s", current.tenant_id)
        raise HTTPException(502, f"Storage upload failed: {e.response['Error']['Message']}")

    # Audit log
    audit = AuditLedger(db)
    await audit.record(
        tenant_id=current.tenant_id,
        action_type=AuditActionType.document_uploaded,
        user_id=current.user_id,
        resource_type="document",
        resource_id=document_id,
        resource_hash=sha256,
        payload={
            "original_filename": file.filename,
            "content_type": file.content_type,
            "size_bytes": len(content),
            "s3_key": s3_key,
        },
    )
    await db.commit()

    logger.info(
        "Document uploaded: tenant=%s document_id=%s size=%d sha256=%s...",
        current.tenant_id, document_id, len(content), sha256[:16]
    )

    return {
        "document_id": document_id,
        "sha256_hash": sha256,
        "s3_key": s3_key,
        "size_bytes": len(content),
        "filename": file.filename,
        "status": "uploaded",
    }


@router.get("/{document_id}")
async def get_document_metadata(
    document_id: str,
    current: CurrentUser = Depends(require_min_role(UserRole.compliance_officer)),
):
    """Return S3 metadata for a previously uploaded document."""
    s3_key = f"tenants/{current.tenant_id}/documents/{document_id}"
    try:
        s3 = _s3_client()
        head = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
        meta = head.get("Metadata", {})
        return {
            "document_id": document_id,
            "s3_key": s3_key,
            "size_bytes": head["ContentLength"],
            "content_type": head["ContentType"],
            "original_name": meta.get("original_name"),
            "sha256_hash": meta.get("sha256"),
            "uploaded_by": meta.get("uploaded_by"),
            "uploaded_at": meta.get("uploaded_at"),
            "last_modified": head["LastModified"].isoformat(),
        }
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            raise HTTPException(404, f"Document {document_id} not found")
        raise HTTPException(502, f"Storage lookup failed: {e.response['Error']['Message']}")