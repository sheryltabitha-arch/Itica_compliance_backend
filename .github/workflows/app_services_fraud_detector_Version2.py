"""
Itica — Fraud Detection Layer
Multi-layer fraud signal detection.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from PIL import Image


class FraudSignalSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class FraudSignal:
    check: str
    severity: FraudSignalSeverity
    score: float
    detail: str
    region: str | None = None


@dataclass
class FraudAssessment:
    is_suspicious: bool
    overall_fraud_score: float
    signals: list[FraudSignal]
    requires_manual_review: bool
    rejection_recommended: bool

    def to_dict(self) -> dict:
        return {
            "is_suspicious": self.is_suspicious,
            "overall_fraud_score": round(self.overall_fraud_score, 4),
            "signal_count": len(self.signals),
            "signals": [
                {
                    "check": s.check,
                    "severity": s.severity.value,
                    "score": round(s.score, 4),
                    "detail": s.detail,
                    "region": s.region
                }
                for s in self.signals
            ],
            "requires_manual_review": self.requires_manual_review,
            "rejection_recommended": self.rejection_recommended,
        }


BLUR_THRESHOLD = 50.0
MIN_WIDTH = 400
MIN_HEIGHT = 280
HARD_REJECT = 0.80
REVIEW_THRESHOLD = 0.40


class FraudDetector:
    def assess(
        self,
        image: Image.Image,
        bounding_boxes: list[dict],
        extracted_fields: dict[str, Any],
        document_type: str = "unknown",
    ) -> FraudAssessment:
        signals: list[FraudSignal] = []
        signals.extend(self._check_quality(image))
        signals.extend(self._check_bboxes(bounding_boxes, image.size))
        signals.extend(self._check_patches(image))

        weights = {
            FraudSignalSeverity.LOW: 0.10,
            FraudSignalSeverity.MEDIUM: 0.30,
            FraudSignalSeverity.HIGH: 0.60,
            FraudSignalSeverity.CRITICAL: 1.00
        }

        if signals:
            raw = sum(s.score * weights.get(s.severity, 0.2) for s in signals) / len(signals)
            crit_boost = 0.3 * len([s for s in signals if s.severity == FraudSignalSeverity.CRITICAL])
            score = min(raw + crit_boost, 1.0)
        else:
            score = 0.0

        return FraudAssessment(
            is_suspicious=score >= REVIEW_THRESHOLD or any(
                s.severity == FraudSignalSeverity.CRITICAL for s in signals
            ),
            overall_fraud_score=score,
            signals=signals,
            requires_manual_review=score >= REVIEW_THRESHOLD,
            rejection_recommended=score >= HARD_REJECT,
        )

    def _check_quality(self, image: Image.Image) -> list[FraudSignal]:
        signals = []
        w, h = image.size
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            signals.append(FraudSignal(
                "low_resolution",
                FraudSignalSeverity.HIGH,
                0.85,
                f"Resolution {w}x{h} below minimum"
            ))
        return signals

    def _check_bboxes(self, bboxes: list[dict], size: tuple) -> list[FraudSignal]:
        signals = []
        for bb in bboxes:
            bbox = bb.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            x1, y1, x2, y2 = bbox
            if x1 < 0 or y1 < 0 or x2 > 1.05 or y2 > 1.05:
                signals.append(FraudSignal(
                    "bbox_out_of_bounds",
                    FraudSignalSeverity.MEDIUM,
                    0.60,
                    f"Bbox outside bounds",
                    bb.get("field", "?")
                ))
        return signals

    def _check_patches(self, image: Image.Image) -> list[FraudSignal]:
        # Stub implementation
        return []