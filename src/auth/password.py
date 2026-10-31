"""Enterprise password security module implementing Argon2id hashing and policy enforcement."""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError

logger = logging.getLogger(__name__)

# Argon2id hasher configured per OWASP Password Storage Guidelines:
# Type: Argon2id, Memory: 64 MB (65536 KiB), Time: 2 iterations, Parallelism: 1 thread
_argon2_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=1,
    hash_len=32,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Hash plaintext password using Argon2id with cryptographically random salt."""
    if not password:
        raise ValueError("Password cannot be empty.")
    return _argon2_hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plaintext password against stored Argon2id hash in constant time."""
    if not plain_password or not hashed_password:
        return False
    try:
        return _argon2_hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception as exc:
        logger.error(f"Unexpected error during password verification: {exc}")
        return False


def validate_password_policy(password: str) -> Tuple[bool, Optional[str]]:
    """Validate password against enterprise security policy.

    Rules:
    - Minimum length: 8 characters
    - Maximum length: 128 characters (DoS prevention)
    - Must contain at least one letter
    - Must contain at least one number or special character
    """
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters in length."

    if len(password) > 128:
        return False, "Password cannot exceed 128 characters in length."

    if not re.search(r"[A-Za-z]", password):
        return False, "Password must contain at least one alphabetic character."

    if not re.search(r"[\d\W_]", password):
        return False, "Password must contain at least one digit or special character."

    return True, None
