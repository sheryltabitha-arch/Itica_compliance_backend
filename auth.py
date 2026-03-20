"""
app/middleware/auth.py

Verifies Auth0 JWT tokens and maps to Supabase users.
Provides CurrentUser class, get_current_user, get_current_user_optional,
require_min_role, UserRole — used by all routers.
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

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "dev-broag6chezsq35rs.us.auth0.com")
AUTH0_API_AUDIENCE = os.environ.get("AUTH0_API_AUDIENCE", "https://itica-compliance-backend.onrender.com")
AUTH0_ALGORITHMS = ["RS256"]
AUTH0_ISSUER = f"https://{AUTH0_DOMAIN}/"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://bzbqhljpehpjmilmchkz.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

security = HTTPBearer(auto_error=False)
_jwks_cache: Optional[dict] = None

ROLE_HIERARCHY = {"user": 0, "analyst": 0, "compliance_officer": 1, "admin": 2}


class UserRole:
    user = "user"
    analyst = "analyst"
    compliance_officer = "compliance_officer"
    admin = "admin"
    HIERARCHY = ROLE_HIERARCHY


class CurrentUser:
    def __init__(self, data: dict):
        self.user_id: str = data.get("id", "")
        self.auth0_id: str = data.get("auth0_id", "")
        self.email: str = data.get("email", "")
        self.name: str = data.get("name", "")
        self.tenant_id: str = str(data.get("tenant_id", ""))
        self.role: str = data.get("role", "user")
        self.picture: str = data.get("picture", "")
        self._raw = data

    def get(self, key, default=None):
        return self._raw.get(key, default)

    def __getitem__(self, key):
        return self._raw[key]

    def __contains__(self, key):
        return key in self._raw


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def get_auth0_public_key(kid: str) -> Optional[dict]:
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
    global _jwks_cache

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


async def get_or_create_user(payload: dict) -> dict:
    supabase = get_supabase()

    auth0_id = payload.get("sub")
    email = payload.get("email", "")
    name = payload.get("name", email)
    picture = payload.get("picture", "")

    result = supabase.table("users").select("*, tenants(*)").eq("auth0_id", auth0_id).execute()
    if result.data:
        user = result.data[0]
        supabase.table("users").update({"last_login": "NOW()"}).eq("id", user["id"]).execute()
        return user

    logger.info(f"New user first login: {email}")
    domain = email.split("@")[-1] if "@" in email else None

    tenant = None
    if domain and domain not in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com"):
        tenant_result = supabase.table("tenants").select("*").eq("domain", domain).execute()
        if tenant_result.data:
            tenant = tenant_result.data[0]

    if not tenant:
        tenant_name = (name.split(" ")[0] + "'s Organisation") if name else (domain or "My Organisation")
        tenant_result = supabase.table("tenants").insert({
            "name": tenant_name,
            "domain": domain,
            "plan": "demo",
        }).execute()
        tenant = tenant_result.data[0]
        logger.info(f"Created new tenant: {tenant['id']} for {email}")

    user_result = supabase.table("users").insert({
        "auth0_id": auth0_id,
        "email": email,
        "name": name,
        "picture": picture,
        "tenant_id": tenant["id"],
        "role": "compliance_officer",
        "last_login": "NOW()",
    }).execute()

    user = user_result.data[0]
    user["tenants"] = tenant
    logger.info(f"Created new user: {user['id']} tenant: {tenant['id']}")
    return user


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authorization token provided",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = await verify_auth0_token(credentials.credentials)
    user_data = await get_or_create_user(payload)
    return CurrentUser(user_data)


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[CurrentUser]:
    if not credentials:
        return None
    try:
        payload = await verify_auth0_token(credentials.credentials)
        user_data = await get_or_create_user(payload)
        return CurrentUser(user_data)
    except HTTPException:
        return None


def require_min_role(min_role):
    min_role_str = min_role.value if hasattr(min_role, "value") else str(min_role)

    async def role_checker(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    ) -> CurrentUser:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No authorization token provided",
            )
        payload = await verify_auth0_token(credentials.credentials)
        user_data = await get_or_create_user(payload)
        user = CurrentUser(user_data)
        if ROLE_HIERARCHY.get(user.role, 0) < ROLE_HIERARCHY.get(min_role_str, 0):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {min_role_str}. Your role: {user.role}",
            )
        return user

    return role_checker
