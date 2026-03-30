"""
app/inference/service.py
HuggingFace Inference API — LayoutLMv3 fine-tuned on FUNSD
"""
from __future__ import annotations

import logging
import os
import requests
from typing import Any

logger = logging.getLogger(__name__)

HF_API_TOKEN = os.environ.get("HUGGINGFACE_API_TOKEN", "")
HF_MODEL = "nielsr/layoutlmv3-finetuned-funsd"
HF_API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"

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


def _query_hf(image_bytes: bytes, question: str) -> dict:
    """Send a single question to HF Inference API."""
    headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
    response = requests.post(
        HF_API_URL,
        headers=headers,
        json={
            "inputs": {
                "image": list(image_bytes),
                "question": question,
            }
        },
        timeout=30,
    )
    if response.status_code == 503:
        raise RuntimeError("Model is loading, please retry in 20 seconds")
    if response.status_code != 200:
        raise RuntimeError(f"HuggingFace API error {response.status_code}: {response.text}")
    return response.json()


def extract_document_fields(image_bytes: bytes) -> dict[str, Any]:
    """
    Run LayoutLMv3 document QA on image bytes.
    Asks each question and collects answers with confidence scores.
    """
    if not HF_API_TOKEN:
        raise RuntimeError("HUGGINGFACE_API_TOKEN not set")

    fields = {}
    confidence_scores = {}

    for question in DOCUMENT_QUESTIONS:
        try:
            result = _query_hf(image_bytes, question)
            answer = result.get("answer", "").strip()
            score = result.get("score", 0.0)

            key = (
                question
                .replace("What is the ", "")
                .replace("?", "")
                .strip()
                .lower()
                .replace(" ", "_")
            )

            if answer and answer.lower() not in ("", "none", "n/a", "null"):
                fields[key] = answer
                confidence_scores[key] = round(float(score), 4)

        except RuntimeError as e:
            if "loading" in str(e).lower():
                raise
            logger.warning(f"Field extraction failed for '{question}': {e}")
            continue
        except Exception as e:
            logger.warning(f"Unexpected error for '{question}': {e}")
            continue

    return {
        "fields": fields,
        "confidence_scores": confidence_scores,
    }


def fetch_document_from_supabase(storage_path: str) -> bytes:
    """Fetch document bytes from Supabase Storage."""
    from app.middleware.auth import get_supabase
    SUPABASE_STORAGE_BUCKET = "kyc-documents"
    try:
        sb = get_supabase()
        response = sb.storage.from_(SUPABASE_STORAGE_BUCKET).download(storage_path)
        return response
    except Exception as e:
        raise RuntimeError(f"Supabase Storage fetch failed: {e}")


# Keep S3 fetch as fallback for any existing documents
def fetch_document_from_s3(s3_key: str) -> bytes:
    """Fetch document bytes from S3 (legacy fallback)."""
    import boto3
    s3 = boto3.client("s3")
    bucket = os.environ.get("S3_BUCKET_DOCUMENTS", "itica-documents-encrypted")
    response = s3.get_object(Bucket=bucket, Key=s3_key)
    return response["Body"].read()
