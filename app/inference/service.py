"""
app/inference/service.py
Mindee SDK v2 — Passport, International ID, Driver's License extraction
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

MINDEE_API_KEY = os.environ.get("MINDEE_API_KEY", "")

# Model IDs from your Mindee account
MINDEE_MODEL_PASSPORT    = "9bad69c5-2747-4ef2-8990-3fb5b4b0bd06"
MINDEE_MODEL_INTL_ID     = "448b128e-a4f2-4684-ac0f-7310d1d0ad14"
MINDEE_MODEL_DRIVERS_LIC = "62cffb49-e145-44d1-9029-e2d3cc40dcbe"

LOW_CONFIDENCE_THRESHOLD = 0.75

CONF_MAP = {
    "certain":       1.0,
    "high":          0.9,
    "medium":        0.7,
    "low":           0.5,
    "very_low":      0.3,
    "not_available": 0.0,
}


def _detect_file_type(image_bytes: bytes) -> str:
    if image_bytes[:4] == b'%PDF':
        return "pdf"
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "jpg"
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    return "jpg"


def _parse_confidence(conf) -> float | None:
    """
    Return a float in [0, 1] or None if confidence is genuinely absent.
    Never fabricate a default — callers must handle None explicitly so
    we don't silently inflate overall_confidence with phantom scores.
    """
    if conf is None:
        return None
    if isinstance(conf, (int, float)):
        return float(conf)
    val = getattr(conf, "value", conf)
    mapped = CONF_MAP.get(str(val).lower())
    return mapped  # None if the string isn't in CONF_MAP


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
    params = InferenceParameters(model_id=model_id, confidence=True)
    input_source = BytesInput(image_bytes, filename=filename)

    response = mindee_client.enqueue_and_get_result(
        InferenceResponse,
        input_source,
        params,
    )

    logger.info("Mindee: response received successfully")
    return response.inference.result.fields


def _parse_fields(raw_fields: dict) -> dict[str, Any]:
    """Parse Mindee fields dict into our standard format."""
    fields: dict[str, Any] = {}
    confidence_scores: dict[str, float] = {}

    def extract(mindee_key: str, our_key: str):
        field = raw_fields.get(mindee_key)
        if not field:
            return
        value = getattr(field, "value", None)
        conf  = _parse_confidence(getattr(field, "confidence", None))
        if value and str(value).lower() not in ("", "none", "n/a", "null"):
            fields[our_key] = str(value).strip()
            # Only store confidence if Mindee actually gave us one
            if conf is not None:
                confidence_scores[our_key] = round(conf, 4)

    # NOTE: given_names and surnames are intentionally NOT extracted here —
    # they are handled below in the name-join block so we get a clean full_name.
    extract("birth_date",        "date_of_birth")
    extract("nationality",       "nationality")
    extract("birth_place",       "place_of_birth")
    extract("expiry_date",       "expiry_date")
    extract("id_number",         "document_number")
    extract("document_number",   "document_number")
    extract("issuance_date",     "issue_date")
    extract("issue_date",        "issue_date")
    extract("gender",            "sex")
    # "issuing_authority" is the correct Mindee key; "country" is the 3-letter
    # country code and should NOT be used as the authority string.
    extract("issuing_authority", "issuing_authority")
    extract("address",           "address")
    extract("mrz1",              "mrz1")
    extract("mrz2",              "mrz2")

    # Build full_name from given_names + surnames (always prefer the joined form)
    given   = raw_fields.get("given_names")
    surname = raw_fields.get("surnames")
    given_val   = str(getattr(given,   "value", "") or "").strip()
    surname_val = str(getattr(surname, "value", "") or "").strip()

    if given_val or surname_val:
        fields["full_name"] = f"{given_val} {surname_val}".strip()
        g_conf = _parse_confidence(getattr(given,   "confidence", None))
        s_conf = _parse_confidence(getattr(surname, "confidence", None))
        # Use the minimum of the two available confidence scores
        available = [c for c in (g_conf, s_conf) if c is not None]
        if available:
            confidence_scores["full_name"] = round(min(available), 4)

    # Overall confidence: mean of all per-field scores that Mindee actually returned
    valid_scores = [v for v in confidence_scores.values() if v is not None]
    overall_confidence = round(sum(valid_scores) / len(valid_scores), 4) if valid_scores else None

    logger.info(f"Mindee: parsed {len(fields)} fields")
    return {
        "fields":             fields,
        "confidence_scores":  confidence_scores,
        "overall_confidence": overall_confidence,
    }


def _select_model(document_type: str) -> str:
    t = (document_type or "").lower()
    if "driver" in t or "license" in t or "licence" in t:
        return MINDEE_MODEL_DRIVERS_LIC
    if "id" in t or "international" in t:
        return MINDEE_MODEL_INTL_ID
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
    filename = f"document.{file_type}"
    model_id = _select_model(document_type)

    raw_fields = _query_mindee(image_bytes, model_id=model_id, filename=filename)
    parsed = _parse_fields(raw_fields)

    fields             = parsed["fields"]
    confidence_scores  = parsed["confidence_scores"]
    overall_confidence = parsed["overall_confidence"]

    low_confidence_fields = [
        key for key, score in confidence_scores.items()
        if score is not None and score < LOW_CONFIDENCE_THRESHOLD
    ]

    logger.info(
        f"Mindee extraction complete: {len(fields)} fields, "
        f"{len(low_confidence_fields)} low confidence"
    )

    return {
        "fields":              fields,
        "confidence_scores":   confidence_scores,
        "overall_confidence":  overall_confidence,
        "low_confidence_fields": low_confidence_fields,
    }


def fetch_document_from_supabase(storage_path: str) -> bytes:
    """Fetch document bytes from Supabase Storage."""
    from app.middleware.auth import get_supabase
    SUPABASE_STORAGE_BUCKET = "kyc-documents"
    try:
        sb = get_supabase()
        response = sb.storage.from_(SUPABASE_STORAGE_BUCKET).download(storage_path)
        if not response:
            raise RuntimeError("Download returned empty response")
        logger.info(f"Supabase download success: {storage_path} ({len(response)} bytes)")
        return response
    except Exception as e:
        raise RuntimeError(f"Supabase Storage fetch failed: {e}")
