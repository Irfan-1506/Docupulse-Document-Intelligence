"""Authentication and identity service managing registration, login, lockout, and sessions."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.jwt import (
    create_access_token,
    generate_refresh_token,
    hash_token,
)
from src.auth.password import (
    hash_password,
    validate_password_policy,
    verify_password,
)
from src.auth.rbac import UserRole
from src.db.models import (
    DEFAULT_TENANT_ID,
    RefreshTokenModel,
    TenantMembershipModel,
    TenantModel,
    UserModel,
)

logger = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_MINUTES = 15


class AuthError(Exception):
    """Base exception for authentication errors."""
    pass


class InvalidCredentialsError(AuthError):
    """Raised when email or password verification fails."""
    pass


class AccountLockedError(AuthError):
    """Raised when an account is temporarily locked out."""
    pass


class InactiveUserError(AuthError):
    """Raised when an account is deactivated."""
    pass


class AuthService:
    """Enterprise authentication and session lifecycle service."""

    @classmethod
    async def register_user(
        cls,
        session: AsyncSession,
        email: str,
        password: str,
        full_name: str = "",
        tenant_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        role: UserRole = UserRole.TENANT_ADMIN,
    ) -> Tuple[UserModel, TenantModel, str, str]:
        """Register a new enterprise user and assign them to a tenant organization.

        Returns:
            Tuple of (user, tenant, access_token, refresh_token)
        """
        clean_email = email.strip().lower()

        # 1. Enforce password complexity policy
        valid_pwd, reason = validate_password_policy(password)
        if not valid_pwd:
            raise ValueError(reason or "Password does not meet enterprise policy.")

        # 2. Check for email uniqueness
        stmt = select(UserModel).where(UserModel.email == clean_email)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            raise ValueError(f"User with email '{clean_email}' already exists.")

        # 3. Resolve or create tenant
        target_tenant: Optional[TenantModel] = None
        if tenant_id:
            target_tenant = (await session.execute(
                select(TenantModel).where(TenantModel.id == tenant_id)
            )).scalar_one_or_none()

        if not target_tenant:
            effective_name = tenant_name or f"{clean_email.split('@')[0].capitalize()} Org"
            slug_base = effective_name.lower().replace(" ", "-")
            slug = f"{slug_base}-{uuid.uuid4().hex[:6]}"
            target_tenant = TenantModel(
                id=str(uuid.uuid4()),
                name=effective_name,
                slug=slug,
                status="ACTIVE",
            )
            session.add(target_tenant)
            await session.flush()

        # 4. Hash password with Argon2id and create user
        pwd_hash = hash_password(password)
        user = UserModel(
            id=str(uuid.uuid4()),
            email=clean_email,
            password_hash=pwd_hash,
            full_name=full_name.strip(),
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.flush()

        # 5. Create membership
        membership = TenantMembershipModel(
            id=str(uuid.uuid4()),
            tenant_id=target_tenant.id,
            user_id=user.id,
            role=role.value,
        )
        session.add(membership)

        # 6. Issue access and refresh tokens
        access_token = create_access_token(
            user_id=user.id,
            email=user.email,
            tenant_id=target_tenant.id,
            role=role.value,
        )
        plain_refresh, token_hash, expires_at = generate_refresh_token()
        refresh_model = RefreshTokenModel(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.add(refresh_model)
        await session.commit()

        logger.info(f"Registered user '{user.email}' under tenant '{target_tenant.name}' with role '{role.value}'.")
        return user, target_tenant, access_token, plain_refresh

    @classmethod
    async def authenticate_user(
        cls,
        session: AsyncSession,
        email: str,
        password: str,
    ) -> Tuple[UserModel, str, str, str, str]:
        """Verify user credentials with lockout protection and return active session tokens.

        Returns:
            Tuple of (user, tenant_id, role, access_token, refresh_token)
        """
        clean_email = email.strip().lower()
        stmt = select(UserModel).where(UserModel.email == clean_email)
        user = (await session.execute(stmt)).scalar_one_or_none()

        if not user:
            # Constant-time dummy verify to mitigate user enumeration timing attacks
            verify_password("dummy-password", "$argon2id$v=19$m=65536,t=2,p=1$c29tZXNhbHQ$5rK0k2gYF0p0N6B9K2f8LQ")
            raise InvalidCredentialsError("Invalid email or password.")

        now = datetime.now(timezone.utc)

        # Check account lockout with SQLite offset-naive compatibility
        if user.locked_until:
            locked_dt = user.locked_until
            if locked_dt.tzinfo is None:
                locked_dt = locked_dt.replace(tzinfo=timezone.utc)
            if locked_dt > now:
                remaining_mins = int((locked_dt - now).total_seconds() / 60) + 1
                raise AccountLockedError(
                    f"Account is temporarily locked due to excessive failed attempts. Please retry in {remaining_mins} minute(s)."
                )

        if not user.is_active:
            raise InactiveUserError("User account has been deactivated.")

        # Verify Argon2id hash
        if not verify_password(password, user.password_hash):
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                user.locked_until = now + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
                logger.warning(f"Account '{user.email}' locked out for {LOCKOUT_DURATION_MINUTES} mins after {user.failed_login_attempts} failures.")
            await session.commit()
            raise InvalidCredentialsError("Invalid email or password.")

        # Reset failed attempts on success
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now

        # Fetch user's primary membership
        mem_stmt = select(TenantMembershipModel).where(TenantMembershipModel.user_id == user.id)
        membership = (await session.execute(mem_stmt)).scalar_one_or_none()

        tenant_id = membership.tenant_id if membership else DEFAULT_TENANT_ID
        role = membership.role if membership else UserRole.VIEWER.value

        # Issue tokens
        access_token = create_access_token(
            user_id=user.id,
            email=user.email,
            tenant_id=tenant_id,
            role=role,
        )
        plain_refresh, token_hash, expires_at = generate_refresh_token()
        refresh_model = RefreshTokenModel(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.add(refresh_model)
        await session.commit()

        logger.info(f"User '{user.email}' authenticated successfully (tenant={tenant_id}, role={role}).")
        return user, tenant_id, role, access_token, plain_refresh

    @classmethod
    async def refresh_session(
        cls,
        session: AsyncSession,
        refresh_token: str,
    ) -> Tuple[str, str]:
        """Validate refresh token, rotate to a new pair, and revoke old token.

        Returns:
            Tuple of (new_access_token, new_refresh_token)
        """
        token_hash = hash_token(refresh_token)
        now = datetime.now(timezone.utc)

        stmt = select(RefreshTokenModel).where(
            and_(
                RefreshTokenModel.token_hash == token_hash,
                RefreshTokenModel.revoked_at.is_(None),
                RefreshTokenModel.expires_at > now,
            )
        )
        token_record = (await session.execute(stmt)).scalar_one_or_none()

        if not token_record:
            raise InvalidCredentialsError("Invalid or expired refresh token.")

        # Revoke old refresh token (Token Rotation)
        token_record.revoked_at = now

        # Fetch user and tenant membership
        user = (await session.execute(
            select(UserModel).where(UserModel.id == token_record.user_id)
        )).scalar_one()

        if not user.is_active:
            raise InactiveUserError("User account is inactive.")

        mem_stmt = select(TenantMembershipModel).where(TenantMembershipModel.user_id == user.id)
        membership = (await session.execute(mem_stmt)).scalar_one_or_none()
        tenant_id = membership.tenant_id if membership else DEFAULT_TENANT_ID
        role = membership.role if membership else UserRole.VIEWER.value

        # Issue rotated tokens
        new_access = create_access_token(
            user_id=user.id,
            email=user.email,
            tenant_id=tenant_id,
            role=role,
        )
        plain_refresh, new_token_hash, expires_at = generate_refresh_token()
        new_token_model = RefreshTokenModel(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=new_token_hash,
            expires_at=expires_at,
        )
        session.add(new_token_model)
        await session.commit()

        return new_access, plain_refresh

    @classmethod
    async def revoke_user_sessions(cls, session: AsyncSession, user_id: str) -> None:
        """Revoke all active refresh tokens for an account (forced logout / password reset)."""
        now = datetime.now(timezone.utc)
        stmt = (
            update(RefreshTokenModel)
            .where(
                and_(
                    RefreshTokenModel.user_id == user_id,
                    RefreshTokenModel.revoked_at.is_(None),
                )
            )
            .values(revoked_at=now)
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(f"Revoked all active sessions for user {user_id}.")
