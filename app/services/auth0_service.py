from __future__ import annotations
import logging, os
from functools import lru_cache
import requests
from jose import JWTError, jwt

logger = logging.getLogger(__name__)
AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")
API_AUDIENCE = os.environ.get("AUTH0_API_AUDIENCE", "")
ALGORITHMS = ["RS256"]
JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"

@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    response = requests.get(JWKS_URL, timeout=10)
    response.raise_for_status()
    return response.json()

def _get_rsa_key(token: str) -> dict:
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")
    if not kid:
        raise JWTError("Token header missing 'kid'")
    for key in _get_jwks().get("keys", []):
        if key.get("kid") == kid:
            return {"kty": key["kty"], "kid": key["kid"], "use": key["use"], "n": key["n"], "e": key["e"]}
    raise JWTError(f"RSA key '{kid}' not found")

def verify_token(token: str) -> dict:
    rsa_key = _get_rsa_key(token)
    return jwt.decode(token, rsa_key, algorithms=ALGORITHMS,
                      audience=API_AUDIENCE, issuer=f"https://{AUTH0_DOMAIN}/")

def get_management_api_token() -> str:
    url = f"https://{AUTH0_DOMAIN}/oauth/token"
    payload = {"client_id": AUTH0_CLIENT_ID, "client_secret": AUTH0_CLIENT_SECRET,
               "audience": f"https://{AUTH0_DOMAIN}/api/v2/", "grant_type": "client_credentials"}
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()
    return response.json().get("access_token")

def get_user_profile(user_id: str, access_token: str) -> dict:
    url = f"https://{AUTH0_DOMAIN}/api/v2/users/{user_id}"
    response = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    response.raise_for_status()
    return response.json()
