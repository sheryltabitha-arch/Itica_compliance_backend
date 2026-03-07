"""
Itica — SQLAlchemy Models
Complete schema for KYC extraction, audit, and compliance.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from sqlalchemy import Column, String, DateTime, Float, Boolean, JSON, Integer, ForeignKey
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class UserRole(str, Enum):
    user = "user"
    compliance_officer = "compliance_officer"
    admin = "admin"


class AuditActionType(str, Enum):
    document_uploaded = "document_uploaded"
    extraction_requested = "extraction_requested"
    extraction_completed = "extraction_completed"
    correction_submitted = "correction_submitted"
    review_completed = "review_completed"
    report_generated = "report_generated"
    model_deployed = "model_deployed"
    user_logged_in = "user_logged_in"
    user_logged_out = "user_logged_out"


class User(Base):
    """User profile synced from Auth0."""
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    tenant_id = Column(String, nullable=False)
    name = Column(String, nullable=True)
    picture = Column(String, nullable=True)
    role = Column(String, default="user")
    email_verified = Column(Boolean, default=False)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExtractionResult(Base):
    """Extraction result from LayoutLMv3."""
    __tablename__ = "extraction_results"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False)
    document_id = Column(String, nullable=False)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    model_version_id = Column(String, ForeignKey("model_registry.id"), nullable=True)
    extracted_fields = Column(JSON)
    confidence_scores = Column(JSON)
    overall_confidence = Column(Float)
    low_confidence_flag = Column(Boolean, default=False)
    requires_human_review = Column(Boolean, default=False)
    inference_time_ms = Column(Float)
    document_type = Column(String)
    document_expired = Column(Boolean, default=False)
    fraud_is_suspicious = Column(Boolean, default=False)
    fraud_rejection_recommended = Column(Boolean, default=False)
    ocr_available = Column(Boolean, default=True)
    output_hash = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditEvent(Base):
    """Immutable audit log."""
    __tablename__ = "audit_events"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False)
    action_type = Column(String, nullable=False)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    resource_type = Column(String, nullable=True)
    resource_id = Column(String, nullable=True)
    resource_hash = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    payload = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExtractionCorrection(Base):
    """Corrections submitted by reviewers."""
    __tablename__ = "extraction_corrections"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False)
    extraction_result_id = Column(String, ForeignKey("extraction_results.id"))
    reviewer_id = Column(String, ForeignKey("users.id"))
    corrections = Column(JSON)
    correction_hash = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReviewTask(Base):
    """Human review tasks."""
    __tablename__ = "review_tasks"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False)
    extraction_result_id = Column(String, ForeignKey("extraction_results.id"))
    status = Column(String)
    assigned_to = Column(String, ForeignKey("users.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ModelRegistry(Base):
    """Model version registry."""
    __tablename__ = "model_registry"

    id = Column(String, primary_key=True)
    model_version = Column(String, unique=True, nullable=False)
    checkpoint = Column(String, nullable=False)
    weights_hash = Column(String)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DriftAlert(Base):
    """Model drift alerts."""
    __tablename__ = "drift_alerts"

    id = Column(String, primary_key=True)
    model_version = Column(String, nullable=False)
    tenant_id = Column(String, nullable=True)
    alert_type = Column(String, nullable=False)
    severity = Column(String)
    metric_name = Column(String)
    current_value = Column(Float)
    baseline_value = Column(Float)
    detail = Column(String)
    triggered_at = Column(DateTime, default=datetime.utcnow)