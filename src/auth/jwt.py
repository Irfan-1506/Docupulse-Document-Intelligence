"""JSON Web Token (JWT) handling and cryptographically secure token generation."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import jwt

logger = logging.getLogger(__name__)

# Security Configuration
ALGORITHM = "HS256"
DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 15
ACCESS_TOKEN_EXPIRE_MINUTES = DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES
DEFAULT_REFRESH_TOKEN_EXPIRE_DAYS = 7

# Secret key resolution with production guard
_RAW_SECRET = os.getenv("JWT_SECRET_KEY") or os.getenv("SECRET_KEY")
_APP_ENV = os.getenv("APP_ENV", "development").lower()

if not _RAW_SECRET:
    if _APP_ENV == "production":
        raise RuntimeError("FATAL: JWT_SECRET_KEY environment variable is mandatory in production!")
    # Development-only generated or deterministic fallback
    _RAW_SECRET = "dev-insecure-secret-key-change-in-production-docupulse-v4.3"

JWT_SECRET_KEY = _RAW_SECRET


def create_access_token(
    user_id: str,
    email: str,
    tenant_id: str,
    role: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Issue a short-lived signed JWT access token for API authorization."""
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES))

    payload: Dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "tenant_id": tenant_id,
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Cryptographically verify signature, claims, and expiration of an access token."""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            raise jwt.InvalidTokenError("Token type must be 'access'.")
        return payload
    except jwt.ExpiredSignatureError:
        raise
    except jwt.InvalidTokenError as exc:
        raise ValueError(f"Invalid access token: {exc}") from exc


def generate_refresh_token() -> Tuple[str, str, datetime]:
    """Generate a high-entropy single-use refresh token and its SHA-256 database hash.

    Returns:
        Tuple of (plain_token, token_hash, expires_at)
    """
    plain_token = secrets.token_urlsafe(48)
    token_hash = hash_token(plain_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=DEFAULT_REFRESH_TOKEN_EXPIRE_DAYS)
    return plain_token, token_hash, expires_at


def hash_token(token: str) -> str:
    """Compute SHA-256 digest of token string for safe database indexing."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
