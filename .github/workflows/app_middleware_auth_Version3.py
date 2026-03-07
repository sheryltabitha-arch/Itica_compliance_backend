"""
Itica — Authentication Middleware
JWT token validation via Auth0, tenant isolation, role enforcement.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError

from app.models.models import UserRole
from app.services.auth0_service import verify_token

logger = logging.getLogger(__name__)

security = HTTPBearer()


class CurrentUser:
    """
    Represents the authenticated user extracted from Auth0 JWT token.
    Injected via Depends(get_current_user).
    """
    def __init__(
        self,
        user_id: str,
        email: str,
        tenant_id: str,
        role: UserRole,
        token: str,
        name: Optional[str] = None,
        picture: Optional[str] = None,
    ):
        self.user_id = user_id
        self.email = email
        self.tenant_id = tenant_id
        self.role = role
        self.token = token
        self.name = name
        self.picture = picture


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> CurrentUser:
    """
    Validate JWT token from Auth0 and extract user claims.

    Token claims expected:
      - sub: Auth0 user ID
      - email: User email
      - tenant_id: Custom claim for tenant isolation
      - roles: Array of role names (optional)

    Raises:
        HTTPException 401: Invalid or expired token
        HTTPException 403: Missing required claims
    """
    token = credentials.credentials

    try:
        payload = verify_token(token)
    except JWTError as e:
        logger.warning(f"Token verification failed: {e}")
        raise HTTPException(401, "Invalid or expired token")

    # Extract claims
    user_id = payload.get("sub")
    email = payload.get("email")
    tenant_id = payload.get("tenant_id")
    name = payload.get("name")
    picture = payload.get("picture")

    # Validate required claims
    if not all([user_id, email, tenant_id]):
        logger.warning(
            f"Token missing required claims. sub={user_id}, email={email}, tenant_id={tenant_id}"
        )
        raise HTTPException(
            403,
            "Token missing required claims (sub, email, tenant_id). "
            "Ensure Auth0 Action/Rule adds tenant_id as a custom claim."
        )

    # Determine role from token claims or default to 'user'
    role_name = "user"
    token_roles = payload.get("roles", [])
    if token_roles:
        if "admin" in token_roles:
            role_name = "admin"
        elif "compliance_officer" in token_roles:
            role_name = "compliance_officer"
        else:
            role_name = "user"

    try:
        role = UserRole[role_name]
    except KeyError:
        logger.warning(f"Unknown role in token: {role_name}")
        role = UserRole.user

    logger.debug(
        f"User authenticated: {email} (user_id={user_id}, tenant={tenant_id}, role={role_name})"
    )

    return CurrentUser(
        user_id=user_id,
        email=email,
        tenant_id=tenant_id,
        role=role,
        token=token,
        name=name,
        picture=picture,
    )


def require_min_role(min_role: UserRole):
    """
    Dependency factory: enforce minimum role requirement.

    Role hierarchy:
        user (0) < compliance_officer (1) < admin (2)
    """
    async def check_role(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        role_hierarchy = {
            UserRole.user: 0,
            UserRole.compliance_officer: 1,
            UserRole.admin: 2,
        }

        current_level = role_hierarchy.get(current.role, 0)
        required_level = role_hierarchy.get(min_role, 0)

        if current_level < required_level:
            logger.warning(
                f"Access denied: user {current.email} role {current.role} < required {min_role}"
            )
            raise HTTPException(
                403,
                f"Insufficient permissions. Required role: {min_role.value}"
            )

        return current

    return check_role