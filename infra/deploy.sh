#!/usr/bin/env bash
# Deploy Polycopy stack on a fresh VPS with Docker + Docker Compose installed.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "No .env found — copying .env.example. Edit it before rerunning."
    cp .env.example .env
    exit 1
fi

echo "Pulling latest images and rebuilding..."
docker compose pull
docker compose build

echo "Applying database migrations..."
docker compose run --rm migrate

echo "Starting stack..."
docker compose up -d

echo "Waiting for backend health..."
for i in $(seq 1 30); do
    if curl -fsS http://localhost/api/health >/dev/null 2>&1; then
        echo "Stack is up. Dashboard: http://localhost/"
        exit 0
    fi
    sleep 2
done

echo "Backend did not become healthy in time. Check: docker compose logs backend" >&2
exit 1
