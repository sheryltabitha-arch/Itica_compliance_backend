"""
Itica — Main Application Entry Point — FIXED

Changes from original:
  1. reports router now at prefix="/api/reports" (was "/reports")
  2. auth router prefix moved here (was split between router and main)
  3. auth0_service import-time crash removed (see auth0_service.py)
  4. Added /api/auth/login and /api/auth/register routes via new auth router
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db.session import init_db, dispose_db
from app.routers import auth, documents, extraction, human_review, reports, health, audit, decisions

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Itica compliance platform (v2.0.0)")

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

    # Auth0 client credentials are needed for login/register
    if not os.environ.get("AUTH0_CLIENT_SECRET"):
        logger.warning("AUTH0_CLIENT_SECRET not set — email/password login will fail")

    logger.info(f"Environment: {os.environ.get('ENVIRONMENT', 'development')} | Log: {log_level}")

    yield

    logger.info("Shutting down Itica platform")

    try:
        from app.inference.service import _ocr_executor
        _ocr_executor.shutdown(wait=True)
    except Exception:
        pass

    try:
        await dispose_db()
    except Exception as e:
        logger.error(f"DB shutdown error: {e}")


app = FastAPI(
    title="Itica KYC Platform API",
    description="Compliance Execution Layer — KYC/AML document verification and audit",
    version="2.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

allowed_origins = os.environ.get(
    "CORS_ORIGINS",
    "https://www.iticacompliance.com,https://iticacompliance.com,http://localhost:3000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "2.0.0"}


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
        "name": "Itica KYC Platform API",
        "version": "2.0.0",
        "docs": "/api/docs",
        "status": "operational",
    }


# ── Routers ───────────────────────────────────────────────────────────────────
# auth router handles: /api/auth/login, /api/auth/register, /api/auth/google,
#                      /api/auth/verify, /api/auth/profile, /api/auth/logout
app.include_router(auth.router,         prefix="/api/auth",     tags=["auth"])
app.include_router(documents.router,                            tags=["documents"])
app.include_router(extraction.router,                           tags=["extraction"])
app.include_router(human_review.router,                         tags=["review"])
app.include_router(reports.router,                              tags=["reports"])  # prefix="/api/reports" in router
app.include_router(health.router,                               tags=["health"])
app.include_router(audit.router,                                tags=["audit"])
app.include_router(decisions.router,                            tags=["decisions"])

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
