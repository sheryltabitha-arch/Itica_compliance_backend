"""
Itica — Main Application Entry Point — v2.2

Changes from v2.1:
  1. Added router: dashboard (metrics, trends, alerts, investigations)
  2. integrations.py gained /connect, /connections, /{vendor}/backfill/resume
     (no main.py change needed for those — same router object)
  Note: billing router deferred — add when first paying customer onboards

v2.4 additions (routers only — app version string left at 2.2.0, matching
how v2.3/customer was added without a version bump):
  - audit_verify: GET /api/v1/audit/verify — recomputes and checks the
    audit_events hash chain from a configured cutover forward
  - audit_gaps: GET /api/v1/audit/gaps — surfaces audit_events_failed rows
  - customer_timeline: GET /api/v1/customers/timeline — full per-customer
    WHO/WHEN/WHAT/WHY/EVIDENCE reconstruction across cases, decisions,
    extractions, and audit_events
  - cases: generic case model (kyc / suspicious_transaction /
    regulatory_order / security_incident) with drill-down and transitions
  - decisions.py, extraction.py, human_review.py, integrations.py,
    webhook.py, sanctions.py were edited in place (hash-chain
    standardization, real dilisense screening, maker-checker) — no new
    imports needed for those, same router objects already registered above
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db.session import init_db, dispose_db
from app.routers import (
    auth,
    documents,
    extraction,
    human_review,
    reports,
    health,
    audit,
    decisions,
)
from app.routers import integrations, webhook, export, dashboard, customer
from app.routers import audit_verify, audit_gaps, customer_timeline, cases
from app.routers import customer_onboarding, network_detection
from app.routers import team

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Itica compliance platform (v2.2.0)")

    try:
        await init_db()
        logger.info("DB init complete")
    except Exception as e:
        logger.warning(f"DB init skipped: {e}")

    required_env = ["AUTH0_DOMAIN", "AUTH0_API_AUDIENCE", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        logger.error(f"Missing required environment variables: {missing}")
        raise RuntimeError(f"Missing required environment variables: {missing}")

    if not os.environ.get("AUTH0_CLIENT_SECRET"):
        logger.warning("AUTH0_CLIENT_SECRET not set — email/password login and Auth0 metadata patching will fail")

    logger.info(f"Environment: {os.environ.get('ENVIRONMENT', 'development')} | Log: {log_level}")

    yield

    logger.info("Shutting down Itica platform")
    try:
        await dispose_db()
    except Exception as e:
        logger.error(f"DB shutdown error: {e}")


app = FastAPI(
    title="Itica KYC Platform API",
    description="Compliance Execution Layer — KYC/AML document verification and audit",
    version="2.2.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
    redirect_slashes=False,
)

allowed_origins = os.environ.get(
    "CORS_ORIGINS",
    "https://www.iticacompliance.com,https://iticacompliance.com,http://localhost:3000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "2.2.0"}


@app.get("/ready")
async def readiness_check():
    try:
        from app.middleware.auth import get_supabase
        sb = get_supabase()
        sb.table("users").select("id").limit(1).execute()
        return {"status": "ready"}
    except Exception as e:
        logger.warning(f"Readiness check failed: {e}")
        return JSONResponse(status_code=503, content={"status": "not_ready", "detail": str(e)})


@app.get("/")
async def root():
    return {
        "name":    "Itica KYC Platform API",
        "version": "2.2.0",
        "docs":    "/api/docs",
        "status":  "operational",
    }


# ── Routers ───────────────────────────────────────────────────────────────────
# Core
app.include_router(auth.router,          prefix="/api/auth",  tags=["auth"])
app.include_router(documents.router,                          tags=["documents"])
app.include_router(extraction.router,                         tags=["extraction"])
app.include_router(human_review.router,                       tags=["review"])
app.include_router(reports.router,                            tags=["reports"])
app.include_router(health.router,                             tags=["health"])
app.include_router(audit.router,                              tags=["audit"])
app.include_router(decisions.router,                          tags=["decisions"])

# v2.1
app.include_router(integrations.router,                       tags=["integrations"])
app.include_router(webhook.router,                            tags=["webhook"])
app.include_router(export.router,                             tags=["export"])

# v2.2
app.include_router(dashboard.router,                          tags=["dashboard"])

# v2.3 — regulation onboarding admin interface
app.include_router(customer.router,                           tags=["customer"])

# v2.4 — audit-chain verification, case model (maker-checker, suspicious
# transaction / regulatory order / security incident cases), full customer
# compliance timeline
app.include_router(audit_verify.router,                       tags=["audit"])
app.include_router(audit_gaps.router,                         tags=["audit"])
app.include_router(customer_timeline.router,                  tags=["customers"])
app.include_router(cases.router,                              tags=["cases"])

# v2.5 — customer onboarding from a KYC extraction, per-customer transactions,
# sanctions-driven risk score. Registered AFTER customer_timeline on purpose.
app.include_router(customer_onboarding.router,                tags=["customer-profiles"])
app.include_router(network_detection.router,                  tags=["network-detection"])

# v2.6 — team invitations
app.include_router(team.router,                               tags=["team"])

logger.info("All routers registered")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8000)),
        workers=1,
        reload=os.environ.get("ENVIRONMENT", "development") == "development",
    )
