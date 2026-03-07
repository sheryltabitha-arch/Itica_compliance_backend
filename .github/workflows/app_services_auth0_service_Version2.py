"""
Itica — Auth0 Integration Service
Handles JWT verification, user profile management, and role mapping.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

import requests
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

# Auth0 Configuration
AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")
API_AUDIENCE = os.environ.get("AUTH0_API_AUDIENCE", "")

if not all([AUTH0_DOMAIN, AUTH0_CLIENT_ID, API_AUDIENCE]):
    raise RuntimeError(
        "Missing Auth0 configuration. Set AUTH0_DOMAIN, AUTH0_CLIENT_ID, and AUTH0_API_AUDIENCE"
    )

ALGORITHMS = ["RS256"]
JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """Fetch Auth0 JWKS (JSON Web Key Set). Cached for 1 hour."""
    try:
        response = requests.get(JWKS_URL, timeout=10)
        response.raise_for_status()
        logger.debug("JWKS refreshed from Auth0")
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch JWKS: {e}")
        raise RuntimeError("Cannot fetch Auth0 JWKS") from e


def _get_rsa_key(token: str) -> dict:
    """Extract the RSA key from JWKS that matches the token's 'kid' header."""
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as e:
        logger.warning(f"Invalid token header: {e}")
        raise JWTError("Invalid token header") from e

    kid = unverified_header.get("kid")
    if not kid:
        raise JWTError("Token header missing 'kid'")

    jwks = _get_jwks()
    rsa_key = None

    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            rsa_key = {
                "kty": key.get("kty"),
                "kid": key.get("kid"),
                "use": key.get("use"),
                "n": key.get("n"),
                "e": key.get("e"),
            }
            break

    if not rsa_key:
        raise JWTError(f"RSA key with kid '{kid}' not found in JWKS")

    return rsa_key


def verify_token(token: str) -> dict:
    """
    Verify and decode an Auth0 JWT token.

    Returns:
        Decoded token payload (claims).

    Raises:
        JWTError: If token is invalid, expired, or claims don't match.
    """
    rsa_key = _get_rsa_key(token)

    try:
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=ALGORITHMS,
            audience=API_AUDIENCE,
            issuer=f"https://{AUTH0_DOMAIN}/",
        )
        logger.debug(f"Token verified for user {payload.get('sub')}")
        return payload
    except JWTError as e:
        logger.warning(f"Token verification failed: {e}")
        raise


def get_user_profile(user_id: str, access_token: str) -> dict:
    """Fetch Auth0 user profile using the Management API."""
    url = f"https://{AUTH0_DOMAIN}/api/v2/users/{user_id}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        logger.debug(f"User profile fetched for {user_id}")
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch user profile: {e}")
        raise


def get_management_api_token() -> str:
    """Get a Management API token using client credentials flow."""
    url = f"https://{AUTH0_DOMAIN}/oauth/token"
    payload = {
        "client_id": AUTH0_CLIENT_ID,
        "client_secret": AUTH0_CLIENT_SECRET,
        "audience": f"https://{AUTH0_DOMAIN}/api/v2/",
        "grant_type": "client_credentials",
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        token = response.json().get("access_token")
        logger.debug("Management API token obtained")
        return token
    except Exception as e:
        logger.error(f"Failed to get Management API token: {e}")
        raise


def get_user_roles(user_id: str, access_token: str) -> list[str]:
    """Fetch user's assigned roles from Auth0."""
    url = f"https://{AUTH0_DOMAIN}/api/v2/users/{user_id}/roles"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        roles = [role.get("name") for role in response.json()]
        logger.debug(f"Roles fetched for {user_id}: {roles}")
        return roles
    except Exception as e:
        logger.warning(f"Failed to fetch user roles: {e}")
        return []