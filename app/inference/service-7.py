"""
app/inference/service.py
Eden AI — Identity Parser (replaces HuggingFace impira/layoutlm-document-qa)
"""
from __future__ import annotations

import base64
import logging
import os
import requests
from typing import Any

logger = logging.getLogger(__name__)

EDEN_AI_API_KEY = os.environ.get("EDEN_AI_API_KEY", "")
EDEN_AI_URL     = "https://api.edenai.run/v2/ocr/identity_parser"

# Provider order: amazon first, microsoft as fallback
PROVIDERS       = ["amazon", "microsoft"]

LOW_CONFIDENCE_THRESHOLD = 0.75

# Maps Eden AI field names → your existing field keys
FIELD_MAP = {
    "full_name":       "full_name",
    "last_name":       "full_name",        # fallback if full_name missing
    "given_names":     "full_name",        # fallback
    "date_of_birth":   "date_of_birth",
    "document_id":     "document_number",
    "expire_date":     "expiry_date",
    "nationality":     "nationality",
    "address":         "address",
    "date_of_issue":   "issue_date",
    "place_of_birth":  "place_of_birth",
    "gender":          "sex",
    "issuing_state":   "issuing_authority",
}


def _to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _query_eden_ai(image_bytes: bytes) -> dict:
    """
    Send image to Eden AI identity parser.
    Returns the raw provider response dict.
    """
    if not EDEN_AI_API_KEY:
        raise RuntimeError("EDEN_AI_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {EDEN_AI_API_KEY}",
        "Content-Type":  "application/json",
    }

    payload = {
        "providers":  ",".join(PROVIDERS),
        "file":       _to_base64(image_bytes),
        "file_type":  "jpg",
    }

    response = requests.post(EDEN_AI_URL, headers=headers, json=payload, timeout=30)

    if response.status_code == 429:
        raise RuntimeError("Eden AI rate limit reached")
    if response.status_code != 200:
        raise RuntimeError(f"Eden AI error {response.status_code}: {response.text}")

    return response.json()


def _parse_eden_response(data: dict) -> dict[str, Any]:
    """
    Parse Eden AI response into fields + confidence_scores.
    Tries providers in order, falls back to next if first fails.
    """
    fields:            dict[str, Any]   = {}
    confidence_scores: dict[str, float] = {}

    for provider in PROVIDERS:
        provider_data = data.get(provider, {})

        # Skip if provider failed
        if provider_data.get("status") == "fail":
            logger.warning(f"Eden AI provider '{provider}' failed: {provider_data.get('error')}")
            continue

        extracted = provider_data.get("extracted_data", [])
        if not extracted:
            continue

        doc_fields = extracted[0].get("fields", {})

        for eden_key, our_key in FIELD_MAP.items():
            if our_key in fields:
                continue  # already populated by higher-priority provider

            field_data = doc_fields.get(eden_key)
            if not field_data:
                continue

            value      = field_data.get("value", "").strip()
            confidence = float(field_data.get("confidence") or 0.80)

            if value and value.lower() not in ("", "none", "n/a", "null"):
                fields[our_key]            = value
                confidence_scores[our_key] = round(confidence, 4)

        # If we got enough fields from this provider, stop
        if len(fields) >= 5:
            logger.info(f"Eden AI: using provider '{provider}', got {len(fields)} fields")
            break

    return {"fields": fields, "confidence_scores": confidence_scores}


def extract_document_fields(image_bytes: bytes) -> dict[str, Any]:
    """
    Extract KYC fields from document image using Eden AI.
    Returns fields, confidence_scores, and low_confidence_fields.
    Drop-in replacement for the HuggingFace implementation.
    """
    raw = _query_eden_ai(image_bytes)
    parsed = _parse_eden_response(raw)

    fields            = parsed["fields"]
    confidence_scores = parsed["confidence_scores"]

    low_confidence_fields = [
        key for key, score in confidence_scores.items()
        if score < LOW_CONFIDENCE_THRESHOLD
    ]

    logger.info(
        f"Eden AI extraction complete: {len(fields)} fields, "
        f"{len(low_confidence_fields)} low confidence"
    )

    return {
        "fields":                fields,
        "confidence_scores":     confidence_scores,
        "low_confidence_fields": low_confidence_fields,
    }


def fetch_document_from_supabase(storage_path: str) -> bytes:
    """Fetch document bytes from Supabase Storage. Unchanged."""
    from app.middleware.auth import get_supabase
    SUPABASE_STORAGE_BUCKET = "kyc-documents"
    try:
        sb       = get_supabase()
        response = sb.storage.from_(SUPABASE_STORAGE_BUCKET).download(storage_path)
        return response
    except Exception as e:
        raise RuntimeError(f"Supabase Storage fetch failed: {e}")
