"""Enterprise structured logging, contextual correlation, and sanitized error categories."""

from __future__ import annotations

import json
import logging
import re
import time
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCategory(str, Enum):
    """Normalized error classification taxonomy for observable failure telemetry."""
    FILE_VALIDATION_ERROR = "FILE_VALIDATION_ERROR"
    OCR_ERROR = "OCR_ERROR"
    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    AI_PROVIDER_ERROR = "AI_PROVIDER_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    REDIS_ERROR = "REDIS_ERROR"
    WORKER_ERROR = "WORKER_ERROR"


# Patterns matching potential credentials, tokens, and authorization headers
SECRET_PATTERNS = [
    re.compile(r"(api[_-]?key|token|secret|password|bearer|authorization)[:=\s]+['\"]?([A-Za-z0-9_\-\.]{8,})['\"]?", re.IGNORECASE),
    re.compile(r"(AIza[0-9A-Za-z-_]{35})"),  # Google API key pattern
    re.compile(r"(sk-[a-zA-Z0-9]{32,})"),     # OpenAI key pattern
]


def sanitize_sensitive_data(message: str) -> str:
    """Mask any accidentally embedded API keys, passwords, or tokens in logs or error strings."""
    if not message:
        return ""
    sanitized = message
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(r"\1: [REDACTED]", sanitized)
    return sanitized


class StructuredLoggerAdapter(logging.LoggerAdapter):
    """Contextual logger attaching document_id, job_id, worker_id, and stage to log lines."""

    def process(self, msg: Any, kwargs: Any) -> tuple[str, Any]:
        context = dict(self.extra or {})
        # Sanitize message
        sanitized_msg = sanitize_sensitive_data(str(msg))

        # Build structured JSON or prefix
        context_items = [f"{k}={v}" for k, v in context.items() if v is not None]
        prefix = f"[{' '.join(context_items)}] " if context_items else ""
        return f"{prefix}{sanitized_msg}", kwargs


def get_structured_logger(
    name: str,
    job_id: Optional[str] = None,
    document_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    stage: Optional[str] = None,
    attempt: Optional[int] = None,
) -> StructuredLoggerAdapter:
    """Factory creating a contextual structured logger adapter."""
    base_logger = logging.getLogger(name)
    extra = {
        "job_id": job_id,
        "doc_id": document_id,
        "worker_id": worker_id,
        "stage": stage,
        "attempt": attempt,
    }
    return StructuredLoggerAdapter(base_logger, extra)
