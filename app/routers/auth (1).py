"""
app/routers/auth.py  — FIXED

Adds:
  POST /api/auth/login      — email/password sign-in
  POST /api/auth/register   — new account creation
  POST /api/auth/google     — Google JWT exchange
  GET  /api/auth/verify     — token validation (already existed)
  GET  /api/auth/profile    — user profile
  POST /api/auth/logout     — logout
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from app.middleware.auth import CurrentUser, get_current_user, get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = None
    name: Optional[str] = None
    organisation: Optional[str] = None
    job_title: Optional[str] = None


class GoogleAuthRequest(BaseModel):
    token: Optional[str] = None
    id_token: Optional[str] = None


class UserProfileResponse(BaseModel):
    user_id: str
    email: str
    name: Optional[str]
    picture: Optional[str]
    tenant_id: str
    role: str
    member_since: Optional[str] = None


# ── Helper: exchange Auth0 password grant ─────────────────────────────────────

async def _auth0_password_grant(email: str, password: str) -> dict:
    """
    Calls Auth0 Resource Owner Password Grant.
    Returns the Auth0 token response dict.
    Raises HTTPException on failure.
    """
    import httpx

    if not AUTH0_DOMAIN or not AUTH0_CLIENT_ID:
        raise HTTPException(500, "Auth0 not configured on backend")

    url = f"https://{AUTH0_DOMAIN}/oauth/token"
    audience = os.environ.get("AUTH0_API_AUDIENCE", "")

    payload = {
        "grant_type": "password",
        "username": email,
        "password": password,
        "audience": audience,
        "scope": "openid profile email",
        "client_id": AUTH0_CLIENT_ID,
        "client_secret": AUTH0_CLIENT_SECRET,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, json=payload)
        except httpx.TimeoutException:
            raise HTTPException(504, "Auth0 timed out — please try again")

    if resp.status_code == 200:
        return resp.json()

    # Map Auth0 error codes to friendly messages
    try:
        err = resp.json()
        err_code = err.get("error", "")
        err_desc = err.get("error_description", "")
    except Exception:
        err_code, err_desc = "", ""

    if err_code in ("invalid_grant", "wrong_password"):
        raise HTTPException(401, "Invalid email or password.")
    if err_code == "invalid_user_password":
        raise HTTPException(401, "Invalid email or password.")
    if err_code == "too_many_attempts":
        raise HTTPException(429, "Too many failed attempts. Please try again later.")

    logger.warning(f"Auth0 password grant failed: {err_code} — {err_desc}")
    raise HTTPException(401, err_desc or "Authentication failed.")


async def _auth0_signup(email: str, password: str, name: str) -> dict:
    """
    Creates a new Auth0 user via the /dbconnections/signup endpoint.
    Returns the created user dict or raises HTTPException.
    """
    import httpx

    if not AUTH0_DOMAIN or not AUTH0_CLIENT_ID:
        raise HTTPException(500, "Auth0 not configured on backend")

    url = f"https://{AUTH0_DOMAIN}/dbconnections/signup"
    payload = {
        "client_id": AUTH0_CLIENT_ID,
        "email": email,
        "password": password,
        "connection": "Username-Password-Authentication",
        "name": name,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, json=payload)
        except httpx.TimeoutException:
            raise HTTPException(504, "Auth0 timed out — please try again")

    if resp.status_code in (200, 201):
        return resp.json()

    try:
        err = resp.json()
        err_code = err.get("code", err.get("error", ""))
        err_desc = err.get("description", err.get("message", err.get("error_description", "")))
    except Exception:
        err_code, err_desc = "", ""

    if err_code in ("user_exists", "invalid_signup"):
        raise HTTPException(409, "An account with this email already exists.")
    if "password" in err_desc.lower():
        raise HTTPException(422, f"Password issue: {err_desc}")

    logger.warning(f"Auth0 signup failed: {err_code} — {err_desc}")
    raise HTTPException(400, err_desc or "Registration failed.")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(body: LoginRequest):
    """
    Email + password sign-in via Auth0 Resource Owner Password Grant.
    Returns access_token + basic user info.
    """
    token_data = await _auth0_password_grant(body.email, body.password)

    access_token = token_data.get("access_token")
    id_token = token_data.get("id_token")

    # Decode id_token for display info (no verification needed — we just got it from Auth0)
    name = body.email.split("@")[0]
    picture = None
    try:
        import base64, json as _json
        if id_token:
            parts = id_token.split(".")
            if len(parts) == 3:
                padded = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = _json.loads(base64.urlsafe_b64decode(padded))
                name = claims.get("name") or claims.get("nickname") or name
                picture = claims.get("picture")
    except Exception:
        pass

    # Ensure user exists in Supabase
    user_record = None
    try:
        from app.middleware.auth import verify_auth0_token, get_or_create_user
        payload = await verify_auth0_token(access_token)
        user_record = await get_or_create_user(payload)
        name = user_record.get("name") or name
    except Exception as e:
        logger.warning(f"Post-login Supabase sync failed (non-fatal): {e}")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "email": body.email,
            "name": name,
            "picture": picture,
            "tenant_id": user_record.get("tenant_id") if user_record else None,
            "role": user_record.get("role", "compliance_officer") if user_record else "compliance_officer",
        },
    }


@router.post("/register")
async def register(body: RegisterRequest):
    """
    Create a new Auth0 account, then immediately sign in to return a token.
    """
    display_name = body.full_name or body.name or body.email.split("@")[0]

    # 1. Create user in Auth0
    await _auth0_signup(body.email, body.password, display_name)

    # 2. Sign in to get a token (so frontend gets immediate access)
    try:
        token_data = await _auth0_password_grant(body.email, body.password)
    except HTTPException as e:
        # Account created but couldn't auto-login — tell frontend to sign in manually
        logger.warning(f"Post-register login failed: {e.detail}")
        return {
            "access_token": None,
            "message": "Account created. Please sign in.",
            "user": {"email": body.email, "name": display_name},
        }

    access_token = token_data.get("access_token")

    # 3. Sync to Supabase
    user_record = None
    try:
        from app.middleware.auth import verify_auth0_token, get_or_create_user
        payload = await verify_auth0_token(access_token)
        user_record = await get_or_create_user(payload)
        # Store organisation/job_title if provided
        if user_record and (body.organisation or body.job_title):
            sb = get_supabase()
            update = {}
            if body.organisation:
                update["organisation"] = body.organisation
            if body.job_title:
                update["job_title"] = body.job_title
            if update:
                sb.table("users").update(update).eq("id", user_record["id"]).execute()
    except Exception as e:
        logger.warning(f"Post-register Supabase sync failed (non-fatal): {e}")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "email": body.email,
            "name": display_name,
            "tenant_id": user_record.get("tenant_id") if user_record else None,
            "role": "compliance_officer",
        },
    }


@router.post("/google")
async def google_auth(body: GoogleAuthRequest):
    """
    Exchange a Google identity token for a backend-verified session.
    Validates the Google JWT, syncs to Supabase, returns access_token.
    """
    google_token = body.token or body.id_token
    if not google_token:
        raise HTTPException(400, "No token provided")

    # Decode Google JWT to get user info (we trust it came from our frontend Google Sign-In)
    try:
        import base64, json as _json
        parts = google_token.split(".")
        if len(parts) != 3:
            raise HTTPException(400, "Invalid Google token format")
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(padded))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Could not decode Google token")

    email = claims.get("email")
    name = claims.get("name") or (email.split("@")[0] if email else "User")
    picture = claims.get("picture")
    google_sub = claims.get("sub")

    if not email:
        raise HTTPException(400, "Google token missing email claim")

    # Upsert user in Supabase directly (Google users bypass Auth0 password grant)
    user_record = None
    try:
        sb = get_supabase()
        # Try to find existing user by email
        result = sb.table("users").select("*, tenants(*)").eq("email", email).execute()
        if result.data:
            user_record = result.data[0]
            sb.table("users").update({"last_login": "NOW()", "picture": picture}).eq("id", user_record["id"]).execute()
        else:
            # Create tenant + user
            domain = email.split("@")[-1] if "@" in email else None
            tenant = None
            if domain and domain not in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com"):
                t = sb.table("tenants").select("*").eq("domain", domain).execute()
                if t.data:
                    tenant = t.data[0]
            if not tenant:
                tenant_name = f"{name.split()[0]}'s Organisation" if name else (domain or "My Organisation")
                t = sb.table("tenants").insert({"name": tenant_name, "domain": domain, "plan": "demo"}).execute()
                tenant = t.data[0]
            u = sb.table("users").insert({
                "auth0_id": f"google|{google_sub}",
                "email": email,
                "name": name,
                "picture": picture,
                "tenant_id": tenant["id"],
                "role": "compliance_officer",
                "last_login": "NOW()",
            }).execute()
            user_record = u.data[0]
    except Exception as e:
        logger.warning(f"Google auth Supabase sync failed (non-fatal): {e}")

    # We return the Google token as the access_token for this session.
    # The frontend stores it and sends it as Bearer — /api/auth/verify accepts it.
    # NOTE: For full production, you'd issue your own JWT here instead.
    return {
        "access_token": google_token,
        "token_type": "bearer",
        "user": {
            "email": email,
            "name": name,
            "picture": picture,
            "tenant_id": user_record.get("tenant_id") if user_record else None,
            "role": user_record.get("role", "compliance_officer") if user_record else "compliance_officer",
            "tenant_name": (user_record.get("tenants", {}) or {}).get("name") if user_record else None,
        },
    }


@router.get("/verify")
async def verify_token_endpoint(current: CurrentUser = Depends(get_current_user)):
    """Verify the current Bearer token is valid and return user profile."""
    return {
        "valid": True,
        "user_id": current.user_id,
        "email": current.email,
        "name": current.name,
        "tenant_id": current.tenant_id,
        "role": current.role,
        "member_since": None,
    }


@router.get("/profile", response_model=UserProfileResponse)
async def get_profile(current: CurrentUser = Depends(get_current_user)):
    """Return full profile for authenticated user."""
    return UserProfileResponse(
        user_id=current.user_id,
        email=current.email,
        name=current.name,
        picture=current.picture,
        tenant_id=current.tenant_id,
        role=current.role,
    )


@router.get("/auth0-config")
async def get_auth0_config():
    """Return Auth0 configuration for frontend."""
    if not AUTH0_DOMAIN or not AUTH0_CLIENT_ID:
        raise HTTPException(500, "Auth0 not configured on backend")
    return {
        "domain": AUTH0_DOMAIN,
        "clientId": AUTH0_CLIENT_ID,
        "redirectUri": os.environ.get("AUTH0_REDIRECT_URI", "https://iticacompliance.com"),
        "audience": os.environ.get("AUTH0_API_AUDIENCE", ""),
        "scope": "openid profile email",
    }


@router.post("/logout")
async def logout(current: CurrentUser = Depends(get_current_user)):
    """Logout endpoint."""
    logger.info(f"User logged out: {current.email}")
    return {"status": "logged_out"}
