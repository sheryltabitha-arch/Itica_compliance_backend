"""
Itica — Field Validator
Validates KYC extracted fields.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, date
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.80

FIELD_CONFIDENCE_THRESHOLDS = {
    "full_name": 0.95,
    "passport_number": 0.97,
    "id_number": 0.97,
    "date_of_birth": 0.90,
    "expiry_date": 0.90,
}


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class ValidationIssue:
    field_name: str
    rule: str
    severity: Severity
    message: str


@dataclass
class FieldResult:
    field_name: str
    raw_value: str
    normalized_value: Optional[str]
    is_valid: bool
    confidence: float
    issues: list[ValidationIssue]


@dataclass
class ValidationReport:
    is_valid: bool
    requires_human_review: bool
    error_count: int
    warning_count: int
    field_results: dict[str,