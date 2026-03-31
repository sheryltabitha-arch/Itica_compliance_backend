"""
app/inference/service.py
Mindee Passport API (replaces Eden AI)
"""
from __future__ import annotations

import logging
import os
import requests
from typing import Any

logger = logging.getLogger(__name__)

MINDEE_API_KEY  = os.environ.get("MINDEE_API_KEY", "")
MINDEE_URL      = "https://api.mindee.net/v1/products/mindee/passport/v1/predict"

LOW_CONFIDENCE_THRESHOLD = 0.75


def _query_mindee(image_bytes: bytes, filename: str = "document.pdf") -> dict:
    """Send document to Mindee Passport API and return raw response."""
    if not MINDEE_API_KEY:
        raise RuntimeError("MINDEE_API_KEY not set")

    logger.info(f"Mindee: sending {len(image_bytes)} bytes, filename={filename}")

    response = requests.post(
        MINDEE_URL,
        headers={"Authorization": f"Token {MINDEE_API_KEY}"},
        files={"document": (filename, image_bytes)},
        timeout=30,
    )

    logger.info(f"Mindee: response status={response.status_code}")

    if response.status_code == 401:
        raise RuntimeError("Mindee API key is invalid or expired")
    if response.status_code == 429:
        raise RuntimeError("Mindee rate limit reached")
    if response.status_code not in (200, 201):
        logger.error(f"Mindee error response: {response.text[:500]}")
        raise RuntimeError(f"Mindee error {response.status_code}: {response.text[:200]}")

    return response.json()


def _parse_mindee_response(data: dict) -> dict[str, Any]:
    """Parse Mindee passport response into fields + confidence_scores."""
    fields:            dict[str, Any]   = {}
    confidence_scores: dict[str, float] = {}

    try:
        prediction = data["document"]["inference"]["prediction"]
    except (KeyError, TypeError) as e:
        logger.error(f"Mindee: unexpected response structure: {e}")
        return {"fields": fields, "confidence_scores": confidence_scores}

    def extract(key: str, our_key: str):
        field = prediction.get(key, {})
        value = field.get("value")
        confidence = float(field.get("confidence") or 0.80)
        if value and str(value).lower() not in ("", "none", "n/a", "null"):
            fields[our_key] = str(value).strip()
            confidence_scores[our_key] = round(confidence, 4)

    extract("given_names",    "full_name")
    extract("surname",        "full_name")   # fallback if given_names missing
    extract("birth_date",     "date_of_birth")
    extract("nationality",    "nationality")
    extract("birth_place",    "place_of_birth")
    extract("expiry_date",    "expiry_date")
    extract("id_number",      "document_number")
    extract("issuance_date",  "issue_date")
    extract("gender",         "sex")
    extract("country",        "issuing_authority")

    # Build full name from given_names + surname if both present
    given  = prediction.get("given_names", {})
    surname = prediction.get("surname", {})
    given_val   = " ".join([g.get("value", "") for g in given]) if isinstance(given, list) else given.get("value", "")
    surname_val = surname.get("value", "") if isinstance(surname, dict) else ""
    if given_val and surname_val:
        fields["full_name"] = f"{given_val} {surname_val}".strip()
        confidence_scores["full_name"] = round(
            min(
                float((given[0] if isinstance(given, list) else given).get("confidence") or 0.80),
                float(surname.get("confidence") or 0.80)
            ), 4
        )
    elif given_val:
        fields["full_name"] = given_val.strip()

    logger.info(f"Mindee: parsed {len(fields)} fields")
    return {"fields": fields, "confidence_scores": confidence_scores}


def extract_document_fields(image_bytes: bytes, storage_path: str | None = None) -> dict[str, Any]:
    """
    Extract KYC fields from document using Mindee Passport API.
    Returns fields, confidence_scores, and low_confidence_fields.
    """
    if not image_bytes:
        raise RuntimeError("Document bytes are empty — fetch from storage may have failed")

    logger.info(f"extract_document_fields: received {len(image_bytes)} bytes")

    # Detect filename for Mindee
    if image_bytes[:4] == b'%PDF':
        filename = "document.pdf"
    elif image_bytes[:3] == b'\xff\xd8\xff':
        filename = "document.jpg"
    elif image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        filename = "document.png"
    else:
        filename = "document.jpg"

    raw    = _query_mindee(image_bytes, filename=filename)
    parsed = _parse_mindee_response(raw)

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
