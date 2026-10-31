#!/usr/bin/env bash
set -e

# ==============================================================================
# DocuPulse Enterprise AP v4.2 - Container Entrypoint
# Handles database migration, service readiness checks, and signal propagation.
# ==============================================================================

echo "=== [DocuPulse AP v4.2] Initializing Container Runtime ==="

# Wait for PostgreSQL if DATABASE_URL contains postgresql
if [[ "$DATABASE_URL" =~ "postgresql" ]]; then
    echo "[Entrypoint] Waiting for PostgreSQL to be ready..."
    python -c "
import asyncio, sys, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def check_db():
    url = os.getenv('DATABASE_URL')
    for attempt in range(1, 31):
        try:
            engine = create_async_engine(url)
            async with engine.connect() as conn:
                await conn.execute(text('SELECT 1'))
            print(f'[Entrypoint] PostgreSQL connection established (attempt {attempt}).')
            return True
        except Exception as e:
            if attempt % 5 == 0:
                print(f'[Entrypoint] Waiting for DB (attempt {attempt}/30): {e}')
            await asyncio.sleep(1)
    print('[Entrypoint] ERROR: Timed out waiting for PostgreSQL.', file=sys.stderr)
    return False

if not asyncio.run(check_db()):
    sys.exit(1)
"
fi

# Run Database Migrations if requested
if [ "$RUN_MIGRATIONS" = "true" ] || [ "$1" = "uvicorn" ]; then
    echo "[Entrypoint] Executing database schema migrations (alembic upgrade head)..."
    if ! alembic upgrade head; then
        if [ "$APP_ENV" = "production" ]; then
            echo "[Entrypoint] ERROR: Production database migration failed! Aborting container startup." >&2
            exit 1
        else
            echo "[Entrypoint] WARNING: Development fallback initializing via init_db()..."
            python -c "import asyncio; from src.db.session import init_db; asyncio.run(init_db())"
        fi
    fi
    echo "[Entrypoint] Database schema verified."
fi

echo "=== [DocuPulse AP v4.2] Handing over execution to: $@ ==="
exec "$@"
