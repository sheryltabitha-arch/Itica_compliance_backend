from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class FraudSignalSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

@dataclass
class FraudAssessment:
    is_suspicious: bool
    overall_fraud_score: float
    signals: list
    requires_manual_review: bool
    rejection_recommended: bool

class FraudDetector:
    def assess(self, image, bounding_boxes, extracted_fields, document_type="unknown"):
        return FraudAssessment(is_suspicious=False, overall_fraud_score=0.0, signals=[],
                               requires_manual_review=False, rejection_recommended=False)
