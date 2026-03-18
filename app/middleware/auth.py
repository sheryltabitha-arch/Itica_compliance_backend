"""
app/middleware/auth.py

Verifies Auth0 JWT tokens and maps to database users.
Creates user + tenant on first login (auto-provisioning).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from supabase import create_client, Client

logger = logging.getLogger(__name__)

# ── Auth0 Config ─────────────────────────────────────────────────────────────
AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "dev-broag6chezsq35rs.us.auth0.com")
AUTH0_API_AUDIENCE = os.environ.get("AUTH0_API_AUDIENCE", "https://itica-compliance-backend.onrender.com")
AUTH0_ALGORITHMS = ["RS256"]
AUTH0_ISSUER = f"https://{AUTH0_DOMAIN}/"

# ── Supabase Config ───────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://bzbqhljpehpjmilmchkz.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

security = HTTPBearer(auto_error=False)

# Cache Auth0 JWKS to avoid fetching on every request
_jwks_cache: Optional[dict] = None


async def get_auth0_public_key(kid: str) -> Optional[dict]:
    """Fetch Auth0 JWKS and find the key matching the token's kid."""
    global _jwks_cache
    if not _jwks_cache:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://{AUTH0_DOMAIN}/.well-known/jwks.json")
            resp.raise_for_status()
            _jwks_cache = resp.json()
    for key in _jwks_cache.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


async def verify_auth0_token(token: str) -> dict:
    """
    Verify Auth0 JWT:
    - Signature using Auth0 public key (RS256)
    - Issuer matches Auth0 domain
    - Audience matches our API
    Returns decoded payload.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token header: {e}",
        )

    kid = unverified_header.get("kid")
    public_key = await get_auth0_public_key(kid)

    if not public_key:
        # Invalidate cache and retry once
        global _jwks_cache
        _jwks_cache = None
        public_key = await get_auth0_public_key(kid)

    if not public_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to find appropriate key",
        )

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=AUTH0_ALGORITHMS,
            audience=AUTH0_API_AUDIENCE,
            issuer=AUTH0_ISSUER,
        )
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token validation failed: {e}",
        )


def get_supabase() -> Client:
    """Get Supabase admin client (service role — bypasses RLS)."""
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def get_or_create_user(payload: dict) -> dict:
    """
    Map Auth0 user to Supabase users table.
    Creates user and tenant on first login.
    """
    supabase = get_supabase()

    auth0_id = payload.get("sub")  # e.g. "google-oauth2|123456"
    email = payload.get("email", "")
    name = payload.get("name", email)
    picture = payload.get("picture", "")

    # Try to find existing user
    result = supabase.table("users").select("*, tenants(*)").eq("auth0_id", auth0_id).execute()

    if result.data:
        user = result.data[0]
        # Update last login
        supabase.table("users").update({"last_login": "NOW()"}).eq("id", user["id"]).execute()
        return user

    # ── First login: auto-provision tenant + user ─────────────────────────
    logger.info(f"New user first login: {email}")

    # Derive tenant from email domain
    domain = email.split("@")[-1] if "@" in email else None

    # Check if tenant already exists for this domain
    tenant = None
    if domain and domain not in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com"):
        tenant_result = supabase.table("tenants").select("*").eq("domain", domain).execute()
        if tenant_result.data:
            tenant = tenant_result.data[0]

    # Create new tenant if needed
    if not tenant:
        tenant_name = name.split(" ")[0] + "'s Organisation" if name else domain or "My Organisation"
        tenant_result = supabase.table("tenants").insert({
            "name": tenant_name,
            "domain": domain,
            "plan": "demo",
        }).execute()
        tenant = tenant_result.data[0]
        logger.info(f"Created new tenant: {tenant['id']} for {email}")

    # Create the user
    user_result = supabase.table("users").insert({
        "auth0_id": auth0_id,
        "email": email,
        "name": name,
        "picture": picture,
        "tenant_id": tenant["id"],
        "role": "analyst",
        "last_login": "NOW()",
    }).execute()

    user = user_result.data[0]
    user["tenants"] = tenant
    logger.info(f"Created new user: {user['id']} tenant: {tenant['id']}")
    return user


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency — verifies token and returns current user.
    Usage: user = Depends(get_current_user)
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authorization token provided",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = await verify_auth0_token(credentials.credentials)
    user = await get_or_create_user(payload)
    return user


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """
    Like get_current_user but returns None instead of raising for unauthenticated requests.
    Useful for endpoints that work in both authenticated and demo modes.
    """
    if not credentials:
        return None
    try:
        payload = await verify_auth0_token(credentials.credentials)
        return await get_or_create_user(payload)
    except HTTPException:
        return None


# Type alias — kept for backwards compatibility with existing routers
CurrentUser = dictasync def get_auth0_public_key(kid: str) -> Optional[dict]:
    """Fetch Auth0 JWKS and find the key matching the token's kid."""
    global _jwks_cache
    if not _jwks_cache:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://{AUTH0_DOMAIN}/.well-known/jwks.json")
            resp.raise_for_status()
            _jwks_cache = resp.json()
    for key in _jwks_cache.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


async def verify_auth0_token(token: str) -> dict:
    """
    Verify Auth0 JWT:
    - Signature using Auth0 public key (RS256)
    - Issuer matches Auth0 domain
    - Audience matches our API
    Returns decoded payload.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token header: {e}",
        )

    kid = unverified_header.get("kid")
    public_key = await get_auth0_public_key(kid)

    if not public_key:
        # Invalidate cache and retry once
        global _jwks_cache
        _jwks_cache = None
        public_key = await get_auth0_public_key(kid)

    if not public_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to find appropriate key",
        )

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=AUTH0_ALGORITHMS,
            audience=AUTH0_API_AUDIENCE,
            issuer=AUTH0_ISSUER,
        )
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token validation failed: {e}",
        )


def get_supabase() -> Client:
    """Get Supabase admin client (service role — bypasses RLS)."""
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def get_or_create_user(payload: dict) -> dict:
    """
    Map Auth0 user to Supabase users table.
    Creates user and tenant on first login.
    """
    supabase = get_supabase()

    auth0_id = payload.get("sub")  # e.g. "google-oauth2|123456"
    email = payload.get("email", "")
    name = payload.get("name", email)
    picture = payload.get("picture", "")

    # Try to find existing user
    result = supabase.table("users").select("*, tenants(*)").eq("auth0_id", auth0_id).execute()

    if result.data:
        user = result.data[0]
        # Update last login
        supabase.table("users").update({"last_login": "NOW()"}).eq("id", user["id"]).execute()
        return user

    # ── First login: auto-provision tenant + user ─────────────────────────
    logger.info(f"New user first login: {email}")

    # Derive tenant from email domain
    domain = email.split("@")[-1] if "@" in email else None

    # Check if tenant already exists for this domain
    tenant = None
    if domain and domain not in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com"):
        tenant_result = supabase.table("tenants").select("*").eq("domain", domain).execute()
        if tenant_result.data:
            tenant = tenant_result.data[0]

    # Create new tenant if needed
    if not tenant:
        tenant_name = name.split(" ")[0] + "'s Organisation" if name else domain or "My Organisation"
        tenant_result = supabase.table("tenants").insert({
            "name": tenant_name,
            "domain": domain,
            "plan": "demo",
        }).execute()
        tenant = tenant_result.data[0]
        logger.info(f"Created new tenant: {tenant['id']} for {email}")

    # Create the user
    user_result = supabase.table("users").insert({
        "auth0_id": auth0_id,
        "email": email,
        "name": name,
        "picture": picture,
        "tenant_id": tenant["id"],
        "role": "analyst",
        "last_login": "NOW()",
    }).execute()

    user = user_result.data[0]
    user["tenants"] = tenant
    logger.info(f"Created new user: {user['id']} tenant: {tenant['id']}")
    return user


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency — verifies token and returns current user.
    Usage: user = Depends(get_current_user)
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authorization token provided",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = await verify_auth0_token(credentials.credentials)
    user = await get_or_create_user(payload)
    return user


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """
    Like get_current_user but returns None instead of raising for unauthenticated requests.
    Useful for endpoints that work in both authenticated and demo modes.
    """
    if not credentials:
        return None
    try:
        payload = await verify_auth0_token(credentials.credentials)
        return await get_or_create_user(payload)
    except HTTPException:
        return None
