# ==============================================================================
# DocuPulse Enterprise AP v4.2 - Production Multi-Stage Dockerfile
# Optimized for Python 3.12, PyMuPDF, OpenCV Headless, Tesseract, and FastAPI
# ==============================================================================

# Stage 1: Build Dependencies & Wheels
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --no-deps --wheel-dir /build/wheels -r requirements.txt


# Stage 2: Final Minimal Production Runtime
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    APP_ENV=production \
    PATH="/home/appuser/.local/bin:$PATH"

# Install essential system libraries:
# - libgl1 & libglib2.0-0 for OpenCV headless
# - tesseract-ocr & language packs for OCR rasterization fallback
# - curl for Docker HEALTHCHECK
# - ca-certificates for secure outbound Gemini/OpenAI API requests
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    tesseract-ocr-eng \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root user for enterprise container security
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g appuser -m -s /bin/bash appuser

# Install Python wheels built in Stage 1
COPY --from=builder /build/wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# Copy application source code and configuration
COPY alembic.ini .
COPY alembic/ alembic/
COPY static/ static/
COPY src/ src/
COPY scripts/ scripts/
COPY docker-entrypoint.sh /docker-entrypoint.sh

# Create runtime directories for uploads and caches
RUN mkdir -p /app/data/uploads /app/data/temp /app/reports && \
    chmod +x /docker-entrypoint.sh && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose FastAPI REST API port
EXPOSE 8000

# Docker Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Set entrypoint and default start command
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
