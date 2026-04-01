"""
app/inference/service.py
Mindee SDK v2 — Passport, International ID, Driver's License extraction
"""
from __future__ import annotations

import logging
import os
import io
from typing import Any

logger = logging.getLogger(__name__)

MINDEE_API_KEY = os.environ.get("MINDEE_API_KEY", "")

# Model IDs from your Mindee account
MINDEE_MODEL_PASSPORT      = "9bad69c5-2747-4ef2-8990-3fb5b4b0bd06"
MINDEE_MODEL_INTL_ID       = "448b128e-a4f2-4684-ac0f-7310d1d0ad14"
MINDEE_MODEL_DRIVERS_LIC   = "62cffb49-e145-44d1-9029-e2d3cc40dcbe"

LOW_CONFIDENCE_THRESHOLD = 0.75


def _detect_file_type(image_bytes: bytes) -> str:
    if image_bytes[:4] == b'%PDF':
        return "pdf"
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "jpg"
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    return "jpg"


def _query_mindee(image_bytes: bytes, model_id: str, filename: str) -> dict:
    """Send document to Mindee using SDK ClientV2."""
    if not MINDEE_API_KEY:
        raise RuntimeError("MINDEE_API_KEY not set")

    try:
        from mindee import ClientV2, InferenceParameters, InferenceResponse, BytesInput
    except ImportError:
        raise RuntimeError("mindee package not installed — add mindee>=4.35.1 to requirements.txt")

    logger.info(f"Mindee: sending {len(image_bytes)} bytes to model {model_id}")

    mindee_client = ClientV2(MINDEE_API_KEY)

    params = InferenceParameters(
        model_id=model_id,
        confidence=True,
    )

    input_source = BytesInput(image_bytes, filename=filename)

    response = mindee_client.enqueue_and_get_result(
        InferenceResponse,
        input_source,
        params,
    )

    logger.info(f"Mindee: response received successfully")
    return response.inference.result.fields


def _parse_fields(raw_fields: dict) -> dict[str, Any]:
    """Parse Mindee fields dict into our standard format."""
    fields:            dict[str, Any]   = {}
    confidence_scores: dict[str, float] = {}

    def extract(mindee_key: str, our_key: str):
        field = raw_fields.get(mindee_key)
        if not field:
            return
        value = getattr(field, "value", None)
        confidence = float(getattr(field, "confidence", None) or 0.80)
        if value and str(value).lower() not in ("", "none", "n/a", "null"):
            fields[our_key] = str(value).strip()
            confidence_scores[our_key] = round(confidence, 4)

    # Common fields across all three document types
    extract("given_names",   "full_name")
    extract("surnames",      "full_name")
    extract("birth_date",    "date_of_birth")
    extract("nationality",   "nationality")
    extract("birth_place",   "place_of_birth")
    extract("expiry_date",   "expiry_date")
    extract("id_number",     "document_number")
    extract("document_number", "document_number")
    extract("issuance_date", "issue_date")
    extract("issue_date",    "issue_date")
    extract("gender",        "sex")
    extract("country",       "issuing_authority")
    extract("address",       "address")
    extract("mrz1",          "mrz1")
    extract("mrz2",          "mrz2")

    # Build full name from given_names + surnames if both present
    given   = raw_fields.get("given_names")
    surname = raw_fields.get("surnames")
    given_val   = str(getattr(given, "value", "") or "").strip()
    surname_val = str(getattr(surname, "value", "") or "").strip()
    if given_val and surname_val:
        fields["full_name"] = f"{given_val} {surname_val}"
        confidence_scores["full_name"] = round(
            min(
                float(getattr(given, "confidence", None) or 0.80),
                float(getattr(surname, "confidence", None) or 0.80),
            ), 4
        )
    elif given_val:
        fields["full_name"] = given_val
    elif surname_val:
        fields["full_name"] = surname_val

    logger.info(f"Mindee: parsed {len(fields)} fields")
    return {"fields": fields, "confidence_scores": confidence_scores}


def _select_model(document_type: str) -> str:
    """Select the correct Mindee model ID based on document type hint."""
    t = (document_type or "").lower()
    if "passport" in t:
        return MINDEE_MODEL_PASSPORT
    if "driver" in t or "license" in t or "licence" in t:
        return MINDEE_MODEL_DRIVERS_LIC
    if "id" in t or "international" in t:
        return MINDEE_MODEL_INTL_ID
    # Default to passport
    return MINDEE_MODEL_PASSPORT


def extract_document_fields(
    image_bytes: bytes,
    storage_path: str | None = None,
    document_type: str = "passport",
) -> dict[str, Any]:
    """
    Extract KYC fields from document using Mindee SDK.
    document_type: 'passport', 'international_id', or 'drivers_license'
    """
    if not image_bytes:
        raise RuntimeError("Document bytes are empty — fetch from storage may have failed")

    logger.info(f"extract_document_fields: {len(image_bytes)} bytes, type={document_type}")

    file_type = _detect_file_type(image_bytes)
    filename  = f"document.{file_type}"
    model_id  = _select_model(document_type)

    raw_fields = _query_mindee(image_bytes, model_id=model_id, filename=filename)
    parsed     = _parse_fields(raw_fields)

    fields            = parsed["fields"]
    confidence_scores = parsed["confidence_scores"]

    low_confidence_fields = [
        key for key, score in confidence_scores.items()
        if score < LOW_CONFIDENCE_THRESHOLD
    ]

    logger.info(
        f"Mindee extraction complete: {len(fields)} fields, "
        f"{len(low_confidence_fields)} low confidence"
    )

    return {
        "fields":                fields,
        "confidence_scores":     confidence_scores,
        "low_confidence_fields": low_confidence_fields,
    }


def fetch_document_from_supabase(storage_path: str) -> bytes:
    """Fetch document bytes from Supabase Storage."""
    from app.middleware.auth import get_supabase
    SUPABASE_STORAGE_BUCKET = "kyc-documents"
    try:
        sb       = get_supabase()
        response = sb.storage.from_(SUPABASE_STORAGE_BUCKET).download(storage_path)
        if not response:
            raise RuntimeError("Download returned empty response")
        logger.info(f"Supabase download success: {storage_path} ({len(response)} bytes)")
        return response
    except Exception as e:
        raise RuntimeError(f"Supabase Storage fetch failed: {e}")
