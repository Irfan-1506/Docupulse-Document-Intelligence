"""Authentication, user registration, session management, and RBAC endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Header, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import UserContext, get_current_user
from src.auth.jwt import ACCESS_TOKEN_EXPIRE_MINUTES
from src.auth.rbac import Permission, UserRole, get_role_permissions
from src.auth.service import (
    AccountLockedError,
    AuthError,
    AuthService,
    InactiveUserError,
    InvalidCredentialsError,
)
from src.db.models import TenantModel, UserModel
from src.db.session import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = Field(..., description="Enterprise email address.")
    password: str = Field(..., min_length=8, description="Secure password complying with complexity policy.")
    full_name: Optional[str] = Field(default="", description="User full display name.")
    tenant_name: Optional[str] = Field(default=None, description="Tenant organization name.")
    tenant_id: Optional[str] = Field(default=None, description="Existing tenant ID to join.")
    role: Optional[str] = Field(default=None, description="Assigned role: SUPER_ADMIN, TENANT_ADMIN, FINANCE_MANAGER, DOCUMENT_REVIEWER, VIEWER.")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = Field(..., description="Registered email address.")
    password: str = Field(..., description="Account password.")


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    refresh_token: Optional[str] = Field(default=None, description="Cryptographic refresh token string.")


class UserInfo(BaseModel):
    id: str
    email: str
    full_name: str
    is_active: bool
    role: str
    tenant_id: str
    permissions: List[str]


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = ACCESS_TOKEN_EXPIRE_MINUTES * 60
    user: UserInfo


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user and assign tenant organization",
)
async def register(
    payload: RegisterRequest,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> AuthResponse:
    """Register a new user account with Argon2id password hashing, create or join tenant, and issue tokens."""
    try:
        # Check if first user in system
        user_count_stmt = select(UserModel.id).limit(1)
        first_user = (await session.execute(user_count_stmt)).first() is None

        # Resolve role
        target_role = UserRole.TENANT_ADMIN if first_user else UserRole.DOCUMENT_REVIEWER
        if payload.role:
            try:
                target_role = UserRole(payload.role.upper())
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid role '{payload.role}'. Must be one of {[r.value for r in UserRole]}",
                )

        user, tenant, access_token, refresh_token = await AuthService.register_user(
            session=session,
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name or "",
            tenant_name=payload.tenant_name,
            tenant_id=payload.tenant_id,
            role=target_role,
        )

        # Set secure HttpOnly cookies for browser frontend
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            samesite="lax",
            secure=False,  # Set True in production HTTPS
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=7 * 24 * 3600,
        )

        permissions = [p.value for p in get_role_permissions(target_role.value)]

        return AuthResponse(
            access_token=access_token,
            user=UserInfo(
                id=user.id,
                email=user.email,
                full_name=user.full_name or "",
                is_active=user.is_active,
                role=target_role.value,
                tenant_id=tenant.id,
                permissions=permissions,
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error during registration: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Registration failed.")


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Authenticate user credentials and issue session tokens",
)
async def login(
    payload: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> AuthResponse:
    """Verify user credentials against Argon2id hash with account lockout protection."""
    try:
        user, tenant_id, role, access_token, refresh_token = await AuthService.authenticate_user(
            session=session,
            email=payload.email,
            password=payload.password,
        )

        # Set secure HttpOnly cookies
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=7 * 24 * 3600,
        )

        permissions = [p.value for p in get_role_permissions(role)]

        return AuthResponse(
            access_token=access_token,
            user=UserInfo(
                id=user.id,
                email=user.email,
                full_name=user.full_name or "",
                is_active=user.is_active,
                role=role,
                tenant_id=tenant_id,
                permissions=permissions,
            ),
        )
    except InvalidCredentialsError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except AccountLockedError as e:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))
    except InactiveUserError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        logger.error(f"Error during login: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication failed.")


@router.post(
    "/refresh",
    response_model=AuthResponse,
    summary="Rotate refresh token and issue fresh access token",
)
async def refresh_session(
    response: Response,
    payload: Optional[RefreshRequest] = None,
    refresh_token_cookie: Optional[str] = Cookie(None, alias="refresh_token"),
    session: AsyncSession = Depends(get_db_session),
) -> AuthResponse:
    """Rotate cryptographic refresh token and issue new 15-minute access token."""
    token = None
    if payload and payload.refresh_token:
        token = payload.refresh_token
    elif refresh_token_cookie:
        token = refresh_token_cookie

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token not provided.")

    try:
        new_access, new_refresh = await AuthService.refresh_session(session, token)

        # Update cookies
        response.set_cookie(
            key="access_token",
            value=new_access,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
        response.set_cookie(
            key="refresh_token",
            value=new_refresh,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=7 * 24 * 3600,
        )

        from src.auth.jwt import decode_access_token
        claims = decode_access_token(new_access)

        user_id = claims.get("sub", "")
        email = claims.get("email", "")
        role = claims.get("role", UserRole.VIEWER.value)
        tenant_id = claims.get("tenant_id", "")
        permissions = [p.value for p in get_role_permissions(role)]

        return AuthResponse(
            access_token=new_access,
            user=UserInfo(
                id=user_id,
                email=email,
                full_name="",
                is_active=True,
                role=role,
                tenant_id=tenant_id,
                permissions=permissions,
            ),
        )
    except InvalidCredentialsError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except Exception as e:
        logger.error(f"Error during token refresh: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token refresh failed.")


@router.post(
    "/logout",
    summary="Revoke session tokens and clear authentication cookies",
)
async def logout(
    response: Response,
    current_user: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, str]:
    """Revoke all server-side active refresh tokens and clear client auth cookies."""
    await AuthService.revoke_user_sessions(session, current_user.user_id)
    response.delete_cookie(key="access_token")
    response.delete_cookie(key="refresh_token")
    return {"message": "Logged out successfully."}


@router.get(
    "/me",
    response_model=UserInfo,
    summary="Get authenticated user identity, role, tenant, and permissions",
)
async def get_me(current_user: UserContext = Depends(get_current_user)) -> UserInfo:
    """Retrieve verified caller context."""
    return UserInfo(
        id=current_user.user_id,
        email=current_user.email,
        full_name="",
        is_active=True,
        role=current_user.role,
        tenant_id=current_user.tenant_id,
        permissions=[p.value for p in current_user.permissions],
    )
