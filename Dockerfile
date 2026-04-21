FROM python:3.12-slim AS base

# postgresql-client gives us pg_dump + pg_restore for the backup + DR tooling
# (see backend/services/backup_service.py and backend/scripts/restore_from_backup.py)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Non-root user for the app process
RUN useradd --create-home --shell /bin/bash app

COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app frontend/ ./frontend/
COPY --chown=app:app alembic/ ./alembic/
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app run.py ./

USER app

EXPOSE 8000

# Production entrypoint: migrate, then serve (no --reload).
# Override CMD for local dev if you want reload.
CMD ["sh", "-c", "alembic upgrade head && uvicorn backend.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
