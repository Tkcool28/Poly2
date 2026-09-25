"""Static checks for the host-facing Compose deployment contract."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def test_compose_ingress_defaults_to_loopback_and_is_configurable():
    compose = read("docker-compose.yml")
    assert '"${POLYCOPY_BIND_ADDRESS:-127.0.0.1}:${POLYCOPY_HTTP_PORT:-8790}:80"' in compose
    nginx = compose.split("\n  nginx:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert '"80:80"' not in nginx
    assert not re.search(r'^\s+-\s+["\']?(?:0\.0\.0\.0:)?80:80', nginx, re.MULTILINE)


def test_postgres_credentials_and_healthcheck_follow_environment():
    compose = read("docker-compose.yml")
    assert "POSTGRES_USER: ${POLYCOPY_POSTGRES_USER:?" in compose
    assert "POSTGRES_PASSWORD: ${POLYCOPY_POSTGRES_PASSWORD:?" in compose
    assert "POSTGRES_DB: ${POLYCOPY_POSTGRES_DB:-polycopy}" in compose
    assert 'pg_isready -U \\"$$POSTGRES_USER\\" -d \\"$$POSTGRES_DB\\"' in compose
    assert "POSTGRES_PASSWORD: polycopy" not in compose


def test_example_credentials_and_safety_defaults():
    example = read(".env.example")
    for line in (
        "POLYCOPY_POSTGRES_USER=",
        "POLYCOPY_POSTGRES_PASSWORD=",
        "POLYCOPY_POSTGRES_DB=polycopy",
        "POLYCOPY_DATABASE_URL=",
        "POLYCOPY_BIND_ADDRESS=127.0.0.1",
        "POLYCOPY_HTTP_PORT=8790",
        "POLYCOPY_PAPER_MODE=true",
        "POLYCOPY_ALLOW_LIVE_TRADING=false",
        "POLYCOPY_ORDER_KILL_SWITCH=true",
    ):
        assert re.search(rf"^{re.escape(line)}$", example, re.MULTILINE)


def test_api_ingress_and_deploy_health_url():
    nginx = read("infra/nginx.conf")
    assert "location /api/ {" in nginx
    assert "proxy_pass http://backend:8000/;" in nginx
    deploy = read("infra/deploy.sh")
    assert "docker compose config --format json" in deploy
    assert 'health_url="http://${ingress_address}/api/health"' in deploy
    assert 'dashboard_url="http://${ingress_address}/"' in deploy
    assert 'curl -fsS "$health_url"' in deploy
    assert 'curl -fsS "$dashboard_url"' in deploy


def test_deploy_waits_for_upstreams_then_refreshes_nginx_and_fails_closed():
    deploy = read("infra/deploy.sh")

    start = deploy.index("docker compose up -d\n")
    upstream_ready = deploy.index(
        "docker compose up -d --wait --wait-timeout 120 backend frontend"
    )
    nginx_refresh = deploy.index(
        "docker compose up -d --force-recreate --no-deps nginx"
    )
    final_health = deploy.index('curl -fsS "$health_url"', nginx_refresh)

    assert start < upstream_ready < nginx_refresh < final_health
    assert "set -euo pipefail" in deploy
    assert "docker compose run --rm migrate" in deploy
    assert "docker compose down" not in deploy
    assert "down -v" not in deploy
    assert "postgres_data" not in deploy
    assert 'echo "nginx/API/dashboard did not become healthy in time." >&2' in deploy
    assert deploy.rstrip().endswith("exit 1")



def test_review_card_uses_explicit_90d_profit_factor():
    approvals = read("frontend/src/pages/Approvals.tsx")
    api = read("frontend/src/lib/api.ts")

    assert "item.score.profit_factor_90d.toFixed(2)" in approvals
    assert "item.score.profit_factor.toFixed(2)" not in approvals
    assert "profit_factor_90d: number | null;" in api
    assert "gross_profit_90d: number | null;" in api
    assert "gross_loss_90d: number | null;" in api
    assert "realized_pnl_90d: number | null;" in api
