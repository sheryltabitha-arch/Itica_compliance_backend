"""
Itica — Main Application Entry Point
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

    # Required env vars — DATABASE_URL excluded (using Supabase, not direct DB)
    required_env = ["AUTH0_DOMAIN", "AUTH0_API_AUDIENCE", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        logger.error(f"Missing required environment variables: {missing}")
        raise RuntimeError(f"Missing required environment variables: {missing}")

    logger.info(f"Environment: {os.environ.get('ENVIRONMENT', 'development')} | Log: {log_level}")

    yield

    logger.info("Shutting down Itica platform")

    try:
        from app.inference.service import _ocr_executor
        _ocr_executor.shutdown(wait=True)
        logger.info("OCR executor shutdown")
    except Exception as e:
        logger.warning(f"OCR executor cleanup: {e}")

    try:
        await dispose_db()
        logger.info("DB connections closed")
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

# CORS — include your Vercel domain
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

# NOTE: TrustedHostMiddleware intentionally removed.
# Render routes through a proxy — that middleware blocks all traffic unless
# you set TRUSTED_HOSTS=* which defeats the purpose. Leave it off.


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "2.0.0"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe — pings Supabase instead of raw SQLAlchemy."""
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
app.include_router(auth.router,         prefix="/api/auth",     tags=["auth"])
app.include_router(documents.router,                            tags=["documents"])
app.include_router(extraction.router,                           tags=["extraction"])
app.include_router(human_review.router,                         tags=["review"])
app.include_router(reports.router,                              tags=["reports"])
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
