#!/bin/sh
set -e

echo "Waiting for PostgreSQL to be ready..."
until pg_isready -d "$DATABASE_URL" 2>/dev/null || pg_isready -h postgres -U "$DB_USER" 2>/dev/null; do
  sleep 1
done
echo "PostgreSQL is ready."

echo "Running Alembic migrations..."
alembic upgrade head

echo "Starting Uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
