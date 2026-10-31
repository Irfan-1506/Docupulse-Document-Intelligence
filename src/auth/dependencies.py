"""FastAPI authentication, tenant context, and RBAC authorization dependencies."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Set

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.jwt import decode_access_token
from src.auth.rbac import Permission, UserRole, get_role_permissions, has_permission
from src.db.models import DEFAULT_TENANT_ID, UserModel
from src.db.session import get_db_session

logger = logging.getLogger(__name__)


@dataclass
class UserContext:
    """Authenticated user context carrying verified identity, tenant, and permissions."""
    user_id: str
    email: str
    tenant_id: str
    role: str
    permissions: Set[Permission]

    def has_permission(self, perm: Permission | str) -> bool:
        norm_perm = Permission(perm) if isinstance(perm, str) else perm
        return norm_perm in self.permissions or UserRole.SUPER_ADMIN.value == self.role


async def get_current_user(
    authorization: Optional[str] = Header(None, description="Bearer <token> authorization header"),
    access_token_cookie: Optional[str] = Cookie(None, alias="access_token", description="Session access token cookie"),
    session: AsyncSession = Depends(get_db_session),
) -> UserContext:
    """Extract and cryptographically verify authentication token from Header or Cookie."""
    raw_token: Optional[str] = None

    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
    elif access_token_cookie:
        raw_token = access_token_cookie.strip()

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(raw_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired. Please log in or refresh your session.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: str = claims.get("sub", "")
    tenant_id: str = claims.get("tenant_id", "")
    role_str: str = claims.get("role", UserRole.VIEWER.value)

    if not user_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token claims.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify user is active in database
    user_stmt = select(UserModel).where(UserModel.id == user_id)
    user = (await session.execute(user_stmt)).scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account no longer exists.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account has been deactivated.",
        )

    permissions = get_role_permissions(role_str)

    return UserContext(
        user_id=user.id,
        email=user.email,
        tenant_id=tenant_id,
        role=role_str,
        permissions=permissions,
    )


async def get_optional_current_user(
    authorization: Optional[str] = Header(None, description="Bearer <token> authorization header"),
    access_token_cookie: Optional[str] = Cookie(None, alias="access_token", description="Session access token cookie"),
    session: AsyncSession = Depends(get_db_session),
) -> UserContext:
    """Extract authenticated context; fallback to default tenant context if auth enforcement is disabled in dev."""
    raw_token: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
    elif access_token_cookie:
        raw_token = access_token_cookie.strip()

    require_auth = os.getenv("DOCUPULSE_REQUIRE_AUTH", "false").lower() == "true"

    if not raw_token:
        if require_auth:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication credentials were not provided.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return UserContext(
            user_id="default-system-user",
            email="system@enterprise.internal",
            tenant_id=DEFAULT_TENANT_ID,
            role=UserRole.TENANT_ADMIN.value,
            permissions=get_role_permissions(UserRole.TENANT_ADMIN.value),
        )

    return await get_current_user(
        authorization=authorization,
        access_token_cookie=access_token_cookie,
        session=session,
    )


def require_permission(perm: Permission | str):
    """Factory creating FastAPI dependencies that enforce specific resource permissions."""
    norm_perm = Permission(perm) if isinstance(perm, str) else perm

    async def permission_guard(current_user: UserContext = Depends(get_optional_current_user)) -> UserContext:
        if not current_user.has_permission(norm_perm):
            logger.warning(
                f"Access denied for user '{current_user.email}' (role={current_user.role}). "
                f"Required permission: '{norm_perm.value}'"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: You do not possess the required permission '{norm_perm.value}'.",
            )
        return current_user

    return permission_guard


def require_role(allowed_roles: list[UserRole | str]):
    """Factory creating FastAPI dependencies that restrict access to specified roles."""
    normalized_allowed = {r.value if isinstance(r, UserRole) else str(r).upper() for r in allowed_roles}

    async def role_guard(current_user: UserContext = Depends(get_optional_current_user)) -> UserContext:
        if current_user.role not in normalized_allowed and current_user.role != UserRole.SUPER_ADMIN.value:
            logger.warning(
                f"Role check failed for user '{current_user.email}' (has '{current_user.role}'). "
                f"Allowed roles: {normalized_allowed}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Your role does not grant access to this resource.",
            )
        return current_user

    return role_guard
