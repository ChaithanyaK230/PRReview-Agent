#!/bin/sh
# Container entrypoint. Migrations run here because Render's free tier has no pre-deploy step;
# `alembic upgrade head` is a no-op when the DB is already current. Fly overrides this per process.
set -e
alembic upgrade head
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips '*'
