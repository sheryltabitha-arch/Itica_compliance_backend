from __future__ import annotations
import hashlib, logging, os, uuid
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from app.middleware.auth import CurrentUser, require_min_role, get_supabase
from app.models.models import UserRole

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
):
    content = await file.read()
    _validate_file(file, content)

    sha256 = hashlib.sha256(content).hexdigest()
    document_id = str(uuid.uuid5(
        uuid.UUID("00000000-0000-0000-0000-000000000000"),
        f"{current.tenant_id}:{sha256}"
    ))

    # Upload to Supabase Storage
    storage_path = f"tenants/{current.tenant_id}/documents/{document_id}"
    try:
        sb = get_supabase()
        response = sb.storage.from_(SUPABASE_STORAGE_BUCKET).upload(
            path=storage_path,
            file=content,
            file_options={
                "content-type": file.content_type or "application/octet-stream",
                "x-upsert": "true",
            }
        )
        # Explicitly check for silent upload failures
        if hasattr(response, 'error') and response.error:
            logger.error(f"Supabase Storage upload error: {response.error}")
            raise HTTPException(502, f"Storage upload failed: {response.error}")

        logger.info(f"Supabase Storage upload success: {storage_path} ({len(content)} bytes)")

    except HTTPException:
        raise
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

    # Audit log directly to Supabase
    try:
        now = datetime.now(timezone.utc)
        prev = sb.table("audit_events").select("hash").eq("tenant_id", str(current.tenant_id)).order("created_at", desc=True).limit(1).execute()
        previous_hash = prev.data[0]["hash"] if prev.data else "GENESIS"
        event_count = sb.table("audit_events").select("id", count="exact").eq("tenant_id", str(current.tenant_id)).execute()
        event_num = (event_count.count or 0) + 1
        sb.table("audit_events").insert({
            "tenant_id": str(current.tenant_id),
            "user_id": str(current.user_id),
            "event_type": "DOCUMENT_UPLOADED",
            "event_id": f"EVT-{event_num:05d}",
            "detail": f"Document uploaded: {file.filename} | Hash: {sha256[:16]}...",
            "hash": sha256,
            "previous_hash": previous_hash,
            "created_at": now.isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"Audit log failed (non-fatal): {e}")

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
