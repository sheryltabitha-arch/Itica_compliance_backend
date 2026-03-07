"""
Itica — Document Type Classifier
Classifies documents and routes to correct LayoutLMv3 checkpoint.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from PIL import Image


class DocumentType(str, Enum):
    PASSPORT = "passport"
    NATIONAL_ID = "national_id"
    DRIVERS_LICENSE = "drivers_license"
    UTILITY_BILL = "utility_bill"
    BANK_STATEMENT = "bank_statement"
    UNKNOWN = "unknown"


CHECKPOINT_MAP: dict[str, str] = {
    DocumentType.PASSPORT: "itica/layoutlmv3-passport",
    DocumentType.NATIONAL_ID: "itica/layoutlmv3-national-id",
    DocumentType.DRIVERS_LICENSE: "itica/layoutlmv3-drivers-license",
    DocumentType.UTILITY_BILL: "itica/layoutlmv3-utility-bill",
    DocumentType.BANK_STATEMENT: "itica/layoutlmv3-bank-statement",
    DocumentType.UNKNOWN: "nielsr/layoutlmv3-finetuned-funsd",
}

_KEYWORD_SIGNALS: dict[str, list[str]] = {
    DocumentType.PASSPORT: ["passport", "nationality", "travel"],
    DocumentType.NATIONAL_ID: ["national id", "identity", "dni"],
    DocumentType.DRIVERS_LICENSE: ["driver", "licence", "license"],
    DocumentType.UTILITY_BILL: ["electricity", "gas", "water"],
    DocumentType.BANK_STATEMENT: ["bank", "statement", "account"],
}


@dataclass
class ClassificationResult:
    document_type: DocumentType
    confidence: float
    checkpoint: str
    mrz_detected: bool
    mrz_raw: str | None
    signals: list[str]
    requires_manual_classification: bool


class DocumentClassifier:
    THRESHOLD = 0.70

    def classify(self, image: Image.Image, ocr_text: str = "") -> ClassificationResult:
        scores: dict[str, float] = {t.value: 0.0 for t in DocumentType}
        signals: list[str] = []

        ocr_lower = ocr_text.lower()
        for doc_type, keywords in _KEYWORD_SIGNALS.items():
            for kw in keywords:
                if kw in ocr_lower:
                    scores[doc_type] += 0.15
                    signals.append(f"keyword:{kw}")

        candidates = {k: v for k, v in scores.items() if k != DocumentType.UNKNOWN}
        if not candidates or max(candidates.values()) == 0:
            best_type = DocumentType.UNKNOWN
            best_score = 0.0
        else:
            best_key = max(candidates, key=candidates.__getitem__)
            best_type = DocumentType(best_key)
            best_score = min(candidates[best_key] / 1.5, 1.0)

        if best_score < self.THRESHOLD:
            best_type = DocumentType.UNKNOWN

        return ClassificationResult(
            document_type=best_type,
            confidence=best_score,
            checkpoint=CHECKPOINT_MAP.get(best_type, CHECKPOINT_MAP[DocumentType.UNKNOWN]),
            mrz_detected=False,
            mrz_raw=None,
            signals=list(set(signals)),
            requires_manual_classification=(best_type == DocumentType.UNKNOWN),
        )