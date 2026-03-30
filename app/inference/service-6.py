"""
app/inference/service.py
HuggingFace Inference API — impira/layoutlm-document-qa (primary)
                            naver-clova-ix/donut-base-finetuned-docvqa (fallback)
"""
from __future__ import annotations

import base64
import logging
import os
import requests
from typing import Any

logger = logging.getLogger(__name__)

HF_API_TOKEN = os.environ.get("HUGGINGFACE_API_TOKEN", "")

# Primary: impira/layoutlm-document-qa — commercially licensed, free inference API
PRIMARY_MODEL    = "impira/layoutlm-document-qa"
PRIMARY_API_URL  = f"https://api-inference.huggingface.co/models/{PRIMARY_MODEL}"

# Fallback: donut — OCR-free, handles skewed/messy documents better
FALLBACK_MODEL   = "naver-clova-ix/donut-base-finetuned-docvqa"
FALLBACK_API_URL = f"https://api-inference.huggingface.co/models/{FALLBACK_MODEL}"

FALLBACK_THRESHOLD = 0.75

DOCUMENT_QUESTIONS = [
    "What is the full name?",
    "What is the date of birth?",
    "What is the document number?",
    "What is the expiry date?",
    "What is the nationality?",
    "What is the address?",
    "What is the issue date?",
    "What is the place of birth?",
    "What is the sex?",
    "What is the issuing authority?",
]


def _question_to_key(question: str) -> str:
    return (
        question
        .replace("What is the ", "")
        .replace("?", "")
        .strip()
        .lower()
        .replace(" ", "_")
    )


def _to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _query_primary(image_bytes: bytes, question: str) -> dict:
    headers  = {"Authorization": f"Bearer {HF_API_TOKEN}"}
    response = requests.post(
        PRIMARY_API_URL,
        headers=headers,
        json={"inputs": {"image": _to_base64(image_bytes), "question": question}},
        timeout=30,
    )
    if response.status_code == 503:
        raise RuntimeError("Model is loading, please retry in 20 seconds")
    if response.status_code != 200:
        raise RuntimeError(f"HuggingFace API error {response.status_code}: {response.text}")
    return response.json()


def _query_fallback(image_bytes: bytes, question: str) -> dict:
    headers  = {"Authorization": f"Bearer {HF_API_TOKEN}"}
    response = requests.post(
        FALLBACK_API_URL,
        headers=headers,
        json={"inputs": {"image": _to_base64(image_bytes), "question": question}},
        timeout=30,
    )
    if response.status_code == 503:
        raise RuntimeError("Fallback model is loading, please retry in 20 seconds")
    if response.status_code != 200:
        raise RuntimeError(f"Fallback HuggingFace API error {response.status_code}: {response.text}")

    data = response.json()

    # Donut returns {"answer": "..."} without a score — normalise
    if isinstance(data, dict) and "answer" in data:
        return {"answer": data["answer"], "score": 0.80}
    if isinstance(data, list) and data:
        first = data[0]
        return {"answer": first.get("answer", ""), "score": first.get("score", 0.80)}
    return {"answer": "", "score": 0.0}


def _query_with_fallback(image_bytes: bytes, question: str) -> dict:
    primary_result = _query_primary(image_bytes, question)
    primary_score  = float(primary_result.get("score", 0.0))

    if primary_score >= FALLBACK_THRESHOLD:
        return primary_result

    logger.info(
        f"Primary confidence {primary_score:.2f} below threshold for '{question}', "
        f"trying fallback model."
    )
    try:
        fallback_result = _query_fallback(image_bytes, question)
        fallback_score  = float(fallback_result.get("score", 0.0))
        if fallback_score > primary_score:
            logger.info(
                f"Fallback won for '{question}': "
                f"{fallback_score:.2f} vs {primary_score:.2f}"
            )
            return fallback_result
    except Exception as e:
        logger.warning(f"Fallback model failed for '{question}': {e}")

    return primary_result


def extract_document_fields(image_bytes: bytes) -> dict[str, Any]:
    """
    Run document QA on image bytes.
    Primary: impira/layoutlm-document-qa
    Fallback: donut-base-finetuned-docvqa (for fields scoring below 0.75)
    Returns fields, confidence_scores, and low_confidence_fields.
    """
    if not HF_API_TOKEN:
        raise RuntimeError("HUGGINGFACE_API_TOKEN not set")

    fields:                dict[str, Any]   = {}
    confidence_scores:     dict[str, float] = {}
    low_confidence_fields: list[str]        = []

    for question in DOCUMENT_QUESTIONS:
        key = _question_to_key(question)
        try:
            result = _query_with_fallback(image_bytes, question)
            answer = result.get("answer", "").strip()
            score  = float(result.get("score", 0.0))

            if answer and answer.lower() not in ("", "none", "n/a", "null"):
                fields[key]            = answer
                confidence_scores[key] = round(score, 4)
                if score < FALLBACK_THRESHOLD:
                    low_confidence_fields.append(key)

        except RuntimeError as e:
            if "loading" in str(e).lower():
                raise
            logger.warning(f"Field extraction failed for '{question}': {e}")
        except Exception as e:
            logger.warning(f"Unexpected error for '{question}': {e}")

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
        return response
    except Exception as e:
        raise RuntimeError(f"Supabase Storage fetch failed: {e}")
