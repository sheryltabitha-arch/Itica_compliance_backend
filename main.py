"""
Itica — Main Application Entry Point
Initializes FastAPI app, registers all routers, middleware,
and startup/shutdown hooks.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.db.session import init_db, dispose_db
from app.routers import auth, documents, extraction, human_review, reports, health

# ── Logging Configuration ────────────────────────────────────────────────────

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan Management ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: Initialize database connections, validate environment.
    Shutdown: Close database connections gracefully.
    """
    logger.info("Starting Itica KYC platform (v2.0.0)")

    # Startup
    try:
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

    # Validate required environment variables
    required_env = [
        "DATABASE_URL",
        "AUTH0_DOMAIN",
        "AUTH0_CLIENT_ID",
        "AUTH0_API_AUDIENCE",
    ]
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        logger.error(f"Missing required environment variables: {missing}")
        raise RuntimeError(f"Missing required environment variables: {missing}")

    logger.info("All required environment variables present")
    logger.info(
        "Environment: %s | Log level: %s",
        os.environ.get("ENVIRONMENT", "development"),
        log_level,
    )

    yield

    # Shutdown
    logger.info("Shutting down Itica KYC platform")

    # Cleanup OCR executor if inference service is loaded
    try:
        from app.inference.service import _ocr_executor
        _ocr_executor.shutdown(wait=True)
        logger.info("OCR executor shutdown")
    except Exception as e:
        logger.warning(f"OCR executor cleanup failed: {e}")

    try:
        await dispose_db()
        logger.info("Database connections closed")
    except Exception as e:
        logger.error(f"Error during database shutdown: {e}")


# ── FastAPI Application ──────────────────────────────────────────────────────

app = FastAPI(
    title="Itica KYC Platform API",
    description="Know Your Customer (KYC) document verification platform with AI-powered extraction and compliance reporting",
    version="2.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


# ── CORS Middleware ──────────────────────────────────────────────────────────

allowed_origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

logger.info(f"CORS origins: {allowed_origins}")


# ── Trusted Host Middleware ──────────────────────────────────────────────────

trusted_hosts = os.environ.get("TRUSTED_HOSTS", "localhost,127.0.0.1").split(",")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)


# ── Global Exception Handlers ────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ── Health Checks ────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Basic liveness probe (Kubernetes/Docker)."""
    return {"status": "healthy", "version": "2.0.0"}


@app.get("/ready")
async def readiness_check():
    """
    Readiness probe — checks if all critical services are available.
    Return 503 if not ready.
    """
    try:
        from app.db.session import get_db_session
        session = await get_db_session()
        await session.execute("SELECT 1")
        await session.close()
        return {"status": "ready"}
    except Exception as e:
        logger.warning(f"Readiness check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "detail": str(e)},
        )


# ── Root Endpoint ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """API root — returns version and documentation link."""
    return {
        "name": "Itica KYC Platform API",
        "version": "2.0.0",
        "docs": "/api/docs",
        "status": "operational",
    }


# ── Router Registration ──────────────────────────────────────────────────────

# Authentication & Authorization
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])

# Document Upload
app.include_router(documents.router, tags=["documents"])

# Extraction & Inference
app.include_router(extraction.router, tags=["extraction"])

# Human Review & Corrections
app.include_router(human_review.router, tags=["review"])

# Compliance Reporting
app.include_router(reports.router, tags=["reports"])

# Health
app.include_router(health.router, tags=["health"])

logger.info("All routers registered")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8000))
    workers = int(os.environ.get("WORKERS", 4))
    environment = os.environ.get("ENVIRONMENT", "development")

    log_config = None if environment == "production" else "default"

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        workers=workers if environment == "production" else 1,
        reload=environment == "development",
        log_config=log_config,
  )
