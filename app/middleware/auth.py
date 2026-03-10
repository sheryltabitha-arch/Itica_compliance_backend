from __future__ import annotations
import logging
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from app.models.models import UserRole
from app.services.auth0_service import verify_token

logger = logging.getLogger(__name__)
security = HTTPBearer()

class CurrentUser:
    def __init__(self, user_id, email, tenant_id, role, token, name=None, picture=None):
        self.user_id = user_id
        self.email = email
        self.tenant_id = tenant_id
        self.role = role
        self.token = token
        self.name = name
        self.picture = picture

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> CurrentUser:
    token = credentials.credentials
    try:
        payload = verify_token(token)
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    user_id = payload.get("sub")
    email = payload.get("email")
    tenant_id = payload.get("tenant_id")
    if not all([user_id, email, tenant_id]):
        raise HTTPException(403, "Token missing required claims")
    role_name = "user"
    token_roles = payload.get("roles", [])
    if "admin" in token_roles:
        role_name = "admin"
    elif "compliance_officer" in token_roles:
        role_name = "compliance_officer"
    try:
        role = UserRole[role_name]
    except KeyError:
        role = UserRole.user
    return CurrentUser(user_id=user_id, email=email, tenant_id=tenant_id,
                       role=role, token=token, name=payload.get("name"),
                       picture=payload.get("picture"))

def require_min_role(min_role: UserRole):
    async def check_role(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        role_hierarchy = {UserRole.user: 0, UserRole.compliance_officer: 1, UserRole.admin: 2}
        if role_hierarchy.get(current.role, 0) < role_hierarchy.get(min_role, 0):
            raise HTTPException(403, f"Insufficient permissions. Required: {min_role.value}")
        return current
    return check_role
