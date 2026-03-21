"""
app/services/auth0_service.py — FIXED

Removed top-level RuntimeError on missing env vars.
That was crashing the entire process on Render cold starts before
env vars were loaded, causing all imports to fail.

Now degrades gracefully: if Auth0 is not configured, verify_token()
raises HTTPException(500) at call time rather than at import time.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

import requests
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")
API_AUDIENCE = os.environ.get("AUTH0_API_AUDIENCE", "")

# ── FIX: removed top-level raise RuntimeError here ──
# Old code crashed at import time if any of these were empty.
# Now we check at call time only.

ALGORITHMS = ["RS256"]


def _jwks_url() -> str:
    domain = os.environ.get("AUTH0_DOMAIN", AUTH0_DOMAIN)
    return f"https://{domain}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """Fetch Auth0 JWKS. Cached for process lifetime."""
    try:
        response = requests.get(_jwks_url(), timeout=10)
        response.raise_for_status()
        logger.debug("JWKS refreshed from Auth0")
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch JWKS: {e}")
        raise RuntimeError("Cannot fetch Auth0 JWKS") from e


def _get_rsa_key(token: str) -> dict:
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise JWTError("Invalid token header") from e

    kid = unverified_header.get("kid")
    if not kid:
        raise JWTError("Token header missing 'kid'")

    jwks = _get_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return {k: key.get(k) for k in ("kty", "kid", "use", "n", "e")}

    raise JWTError(f"RSA key with kid '{kid}' not found in JWKS")


def verify_token(token: str) -> dict:
    """
    Verify and decode an Auth0 JWT token.
    Raises JWTError on failure.
    """
    domain = os.environ.get("AUTH0_DOMAIN", AUTH0_DOMAIN)
    audience = os.environ.get("AUTH0_API_AUDIENCE", API_AUDIENCE)

    if not domain or not audience:
        raise JWTError("Auth0 not configured — AUTH0_DOMAIN or AUTH0_API_AUDIENCE missing")

    rsa_key = _get_rsa_key(token)

    payload = jwt.decode(
        token,
        rsa_key,
        algorithms=ALGORITHMS,
        audience=audience,
        issuer=f"https://{domain}/",
    )
    logger.debug(f"Token verified for user {payload.get('sub')}")
    return payload


def get_user_profile(user_id: str, access_token: str) -> dict:
    domain = os.environ.get("AUTH0_DOMAIN", AUTH0_DOMAIN)
    url = f"https://{domain}/api/v2/users/{user_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_management_api_token() -> str:
    domain = os.environ.get("AUTH0_DOMAIN", AUTH0_DOMAIN)
    client_id = os.environ.get("AUTH0_CLIENT_ID", AUTH0_CLIENT_ID)
    client_secret = os.environ.get("AUTH0_CLIENT_SECRET", AUTH0_CLIENT_SECRET)
    url = f"https://{domain}/oauth/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "audience": f"https://{domain}/api/v2/",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json().get("access_token")


def get_user_roles(user_id: str, access_token: str) -> list[str]:
    domain = os.environ.get("AUTH0_DOMAIN", AUTH0_DOMAIN)
    url = f"https://{domain}/api/v2/users/{user_id}/roles"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        resp.raise_for_status()
        return [r.get("name") for r in resp.json()]
    except Exception as e:
        logger.warning(f"Failed to fetch user roles: {e}")
        return []
