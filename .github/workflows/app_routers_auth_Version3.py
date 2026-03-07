"""
Itica — Authentication Router
Handles Auth0 login/logout, token exchange, and user profile.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, get_current_user
from app.services.auth0_service import get_management_api_token, get_user_profile

logger = logging.getLogger(__name__)
router = APIRouter()

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "")


class UserProfileResponse(BaseModel):
    user_id: str
    email: str
    name: Optional[str]
    picture: Optional[str]
    tenant_id: str
    role: str
    email_verified: bool


@router.get("/auth0-config")
async def get_auth0_config():
    """Return Auth0 configuration for frontend."""
    if not AUTH0_DOMAIN or not AUTH0_CLIENT_ID:
        raise HTTPException(500, "Auth0 not configured on backend")

    return {
        "domain": AUTH0_DOMAIN,
        "clientId": AUTH0_CLIENT_ID,
        "redirectUri": os.environ.get("AUTH0_REDIRECT_URI", "http://localhost:3000/callback"),
        "audience": os.environ.get("AUTH0_API_AUDIENCE", ""),
        "scope": "openid profile email",
    }


@router.get("/profile", response_model=UserProfileResponse)
async def get_user_profile_endpoint(current: CurrentUser = Depends(get_current_user)):
    """Get authenticated user's profile."""
    try:
        mgmt_token = get_management_api_token()
        profile = get_user_profile(current.user_id, mgmt_token)

        return UserProfileResponse(
            user_id=current.user_id,
            email=current.email,
            name=current.name or profile.get("name"),
            picture=current.picture or profile.get("picture"),
            tenant_id=current.tenant_id,
            role=current.role.value,
            email_verified=profile.get("email_verified", False),
        )
    except Exception as e:
        logger.error(f"Failed to fetch user profile: {e}")
        raise HTTPException(500, "Failed to fetch user profile")


@router.post("/logout")
async def logout(current: CurrentUser = Depends(get_current_user)):
    """Logout endpoint."""
    logger.info(f"User logged out: {current.email} (tenant={current.tenant_id})")
    return {"status": "logged_out"}


@router.get("/verify")
async def verify_token(current: CurrentUser = Depends(get_current_user)):
    """Verify that the current token is still valid."""
    return {
        "valid": True,
        "user_id": current.user_id,
        "email": current.email,
        "tenant_id": current.tenant_id,
        "role": current.role.value,
    }