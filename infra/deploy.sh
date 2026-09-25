#!/usr/bin/env bash
# Deploy Polycopy stack on a fresh VPS with Docker + Docker Compose installed.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "No .env found — copying .env.example. Edit it before rerunning."
    cp .env.example .env
    exit 1
fi

# Resolve the same published address and port that Compose will use from .env.
ingress_address=$(docker compose config --format json | python3 -c '
import json, sys
port = json.load(sys.stdin)["services"]["nginx"]["ports"][0]
host = port["host_ip"]
if ":" in host:
    host = f"[{host}]"
print("{}:{}".format(host, port["published"]))
')
health_url="http://${ingress_address}/api/health"
dashboard_url="http://${ingress_address}/"

echo "Pulling latest images and rebuilding..."
docker compose pull
docker compose build

echo "Applying database migrations..."
docker compose run --rm migrate

echo "Starting stack..."
docker compose up -d

echo "Waiting for backend and frontend health..."
docker compose up -d --wait --wait-timeout 120 backend frontend

# nginx resolves Compose service names when it starts. If backend/frontend were
# recreated above while an unchanged nginx container was retained, nginx can
# keep stale upstream addresses. Recreate nginx only after both upstreams are
# healthy so it resolves their current addresses every deployment.
echo "Refreshing nginx upstream addresses..."
docker compose up -d --force-recreate --no-deps nginx

echo "Waiting for nginx/API/dashboard health..."
for i in $(seq 1 30); do
    if curl -fsS "$health_url" >/dev/null 2>&1 \
        && curl -fsS "$dashboard_url" >/dev/null 2>&1; then
        echo "Stack is up. Dashboard: http://${ingress_address}/"
        exit 0
    fi
    sleep 2
done

echo "nginx/API/dashboard did not become healthy in time." >&2
echo "Check: docker compose logs nginx backend frontend" >&2
exit 1
