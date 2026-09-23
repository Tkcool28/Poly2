"""Static checks for the production Compose and operations contract."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LONG_RUNNING = ("postgres", "redis", "backend", "bot", "frontend", "nginx")


def read(path: str) -> str:
    return (ROOT / path).read_text()


def services() -> dict[str, str]:
    compose = read("docker-compose.yml").split("\nvolumes:\n", 1)[0]
    sections = re.split(r"\n  ([a-z]+):\n", compose)
    return dict(zip(sections[1::2], sections[2::2], strict=True))


def test_restarts_and_bounded_per_service_logging():
    parts = services()
    assert set(parts) == {*LONG_RUNNING, "migrate"}
    for name in LONG_RUNNING:
        body = parts[name]
        assert re.search(r"^    restart: unless-stopped$", body, re.MULTILINE), name
        assert re.search(
            r'^    logging:\n      driver: json-file\n      options:\n'
            r'        max-size: "10m"\n        max-file: "5"$',
            body,
            re.MULTILINE,
        ), name
    assert re.search(r'^    restart: "no"$', parts["migrate"], re.MULTILINE)
    assert "logging:" not in parts["migrate"]


def test_frontend_and_nginx_have_local_http_checks_and_bot_omits_one():
    parts = services()
    for name in ("frontend", "nginx"):
        assert "healthcheck:" in parts[name]
        assert '"wget", "-q", "-O", "/dev/null"' in parts[name]
    assert '"http://127.0.0.1/"' in parts["frontend"]
    assert '"http://127.0.0.1/api/health"' in parts["nginx"]
    assert "healthcheck:" not in parts["bot"]


def test_systemd_stack_and_backup_units():
    stack = read("infra/systemd/polycopy.service")
    backup = read("infra/systemd/poly2-backup.service")
    timer = read("infra/systemd/poly2-backup.timer")
    assert "WorkingDirectory=/opt/poly2" in stack
    assert "ExecStart=/usr/bin/docker compose up -d" in stack
    assert "ExecStop=/usr/bin/docker compose stop" in stack
    assert "WorkingDirectory=/opt/poly2" in backup
    assert "User=root" in backup
    assert "ExecStart=/opt/poly2/infra/backup-postgres.sh" in backup
    assert "OnCalendar=*-*-* 04:15:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "Unit=poly2-backup.service" in timer


def test_backup_and_verification_scripts_are_scoped_and_fail_closed():
    backup = read("infra/backup-postgres.sh")
    verify = read("infra/verify-postgres-backup.sh")
    assert "set -euo pipefail" in backup and "set -euo pipefail" in verify
    assert "umask 077" in backup and "umask 077" in verify
    assert "POLY2_BACKUP_KEEP:-14" in backup
    assert "POLY2_BACKUP_DIR:-/var/backups/poly2" in backup
    assert "pg_dump" in backup and "gzip -t" in backup
    assert "mktemp" in backup and "trap " in backup
    assert "flock -n" in backup
    assert "poly2-[0-9]{8}T[0-9]{6}Z" in backup
    assert "POSTGRES_PASSWORD" not in backup + verify
    assert "poly2_restore_verify_" in verify
    assert "created=true" in verify and "if [[ $created == true ]]" in verify
    assert "ON_ERROR_STOP=1" in verify
    assert "dropdb" in verify and "to_regclass" in verify
    assert "docker compose down" not in backup + verify
