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

echo "Pulling latest images and rebuilding..."
docker compose pull
docker compose build

echo "Applying database migrations..."
docker compose run --rm migrate

echo "Starting stack..."
docker compose up -d

echo "Waiting for backend health..."
for i in $(seq 1 30); do
    if curl -fsS "$health_url" >/dev/null 2>&1; then
        echo "Stack is up. Dashboard: http://${ingress_address}/"
        exit 0
    fi
    sleep 2
done

echo "Backend did not become healthy in time. Check: docker compose logs backend" >&2
exit 1
