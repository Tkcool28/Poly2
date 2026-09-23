#!/usr/bin/env bash
# Restore one backup into an isolated temporary database, then drop it.
set -euo pipefail
umask 077

if (( EUID != 0 )) || (( $# != 1 )) || [[ $1 != /* || ! -f $1 ]]; then
    echo "Usage (as root): $0 /var/backups/poly2/poly2-YYYYMMDDTHHMMSSZ.sql.gz" >&2
    exit 1
fi
if [[ -L $1 || ! ${1##*/} =~ ^poly2-[0-9]{8}T[0-9]{6}Z\.sql\.gz$ ]]; then
    echo "Verification requires a regular Poly2 timestamped backup file." >&2
    exit 1
fi
cd "$(dirname "$0")/.."
backup=$1
gzip -t "$backup"

verify_db="poly2_restore_verify_$(date -u +%Y%m%dT%H%M%SZ)_$$"
created=false
cleanup() {
    status=$?
    trap - EXIT
    if [[ $created == true ]]; then
        if ! docker compose exec -T postgres sh -c \
            'exec dropdb -U "$POSTGRES_USER" "$1"' sh "$verify_db"; then
            echo "WARNING: Could not remove temporary database $verify_db; remove it manually." >&2
            exit 1
        fi
    fi
    if (( status != 0 )); then
        echo "Backup restore verification failed." >&2
    fi
    exit "$status"
}
trap cleanup EXIT

# A collision fails createdb and is never dropped by this script.
docker compose exec -T postgres sh -c \
    'exec createdb -U "$POSTGRES_USER" "$1"' sh "$verify_db"
created=true
gzip -cd "$backup" | docker compose exec -T postgres sh -c \
    'exec psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"' sh "$verify_db"
docker compose exec -T postgres sh -c \
    'psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c "SELECT to_regclass('\''public.alembic_version'\'') IS NOT NULL"' \
    sh "$verify_db" | grep -qx t
echo "Restore verified in temporary database $verify_db; cleaning up."
