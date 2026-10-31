"""Centralized Role-Based Access Control (RBAC) and permission mapping."""

from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class UserRole(str, Enum):
    """Enterprise hierarchical user roles."""
    SUPER_ADMIN = "SUPER_ADMIN"
    TENANT_ADMIN = "TENANT_ADMIN"
    FINANCE_MANAGER = "FINANCE_MANAGER"
    DOCUMENT_REVIEWER = "DOCUMENT_REVIEWER"
    VIEWER = "VIEWER"


class Permission(str, Enum):
    """Standardized resource-level operational permissions."""
    DOCUMENT_CREATE = "document:create"
    DOCUMENT_READ = "document:read"
    DOCUMENT_REVIEW = "document:review"
    DOCUMENT_APPROVE = "document:approve"
    DOCUMENT_REPROCESS = "document:reprocess"
    DOCUMENT_DELETE = "document:delete"
    AUDIT_READ = "audit:read"
    USER_MANAGE = "user:manage"
    TENANT_MANAGE = "tenant:manage"


# Mapping from UserRole to explicit set of permitted operations
ROLE_PERMISSIONS: Dict[UserRole, Set[Permission]] = {
    UserRole.SUPER_ADMIN: {
        Permission.DOCUMENT_CREATE,
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_REVIEW,
        Permission.DOCUMENT_APPROVE,
        Permission.DOCUMENT_REPROCESS,
        Permission.DOCUMENT_DELETE,
        Permission.AUDIT_READ,
        Permission.USER_MANAGE,
        Permission.TENANT_MANAGE,
    },
    UserRole.TENANT_ADMIN: {
        Permission.DOCUMENT_CREATE,
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_REVIEW,
        Permission.DOCUMENT_APPROVE,
        Permission.DOCUMENT_REPROCESS,
        Permission.DOCUMENT_DELETE,
        Permission.AUDIT_READ,
        Permission.USER_MANAGE,
        Permission.TENANT_MANAGE,
    },
    UserRole.FINANCE_MANAGER: {
        Permission.DOCUMENT_CREATE,
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_REVIEW,
        Permission.DOCUMENT_APPROVE,
        Permission.DOCUMENT_REPROCESS,
        Permission.AUDIT_READ,
    },
    UserRole.DOCUMENT_REVIEWER: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_REVIEW,
        Permission.AUDIT_READ,
    },
    UserRole.VIEWER: {
        Permission.DOCUMENT_READ,
        Permission.AUDIT_READ,
    },
}


def get_role_permissions(role: str | UserRole) -> Set[Permission]:
    """Return explicit permission set assigned to a role."""
    try:
        norm_role = UserRole(role.upper()) if isinstance(role, str) else role
        return ROLE_PERMISSIONS.get(norm_role, set())
    except (ValueError, KeyError):
        return set()


def has_permission(role: str | UserRole, permission: str | Permission) -> bool:
    """Check if a given role is granted a specific resource permission."""
    perms = get_role_permissions(role)
    norm_perm = Permission(permission) if isinstance(permission, str) else permission
    return norm_perm in perms
