#!/usr/bin/env bash
# Logical backup of the running Compose PostgreSQL service. Run as root.
set -euo pipefail
umask 077

if (( EUID != 0 )); then
    echo "Run PostgreSQL backups as root." >&2
    exit 1
fi

cd "$(dirname "$0")/.."
backup_dir=${POLY2_BACKUP_DIR:-/var/backups/poly2}
keep=${POLY2_BACKUP_KEEP:-14}
if [[ ! $keep =~ ^[1-9][0-9]*$ ]]; then
    echo "POLY2_BACKUP_KEEP must be a positive integer." >&2
    exit 1
fi
if [[ $backup_dir != /* || $backup_dir == / ]]; then
    echo "POLY2_BACKUP_DIR must be an absolute directory below /, not /." >&2
    exit 1
fi
if [[ -L $backup_dir ]]; then
    echo "Backup directory must not be a symbolic link." >&2
    exit 1
fi
install -d -m 0700 "$backup_dir"
if [[ $(stat -c %u "$backup_dir") != 0 ]]; then
    echo "Backup directory must be owned by root." >&2
    exit 1
fi
chmod 0700 "$backup_dir"
backup_dir=$(cd "$backup_dir" && pwd -P)

# Avoid overlapping backups and retention passes.
exec 9>"$backup_dir/.poly2-backup.lock"
flock -n 9 || { echo "Another Poly2 backup is running." >&2; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$backup_dir/poly2-$stamp.sql.gz"
if [[ -e $target ]]; then
    echo "Backup already exists for $stamp; refusing to overwrite it." >&2
    exit 1
fi
partial=$(mktemp "$backup_dir/.poly2-$stamp.XXXXXXXX.partial")
trap 'status=$?; if (( status != 0 )); then echo "PostgreSQL backup failed." >&2; fi; if [[ -n ${partial:-} ]]; then unlink "$partial"; fi' EXIT

# Compose supplies POSTGRES_USER/POSTGRES_DB inside the container from .env.
# pipefail rejects both pg_dump and gzip failures before publishing the file.
docker compose exec -T postgres sh -c \
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
    | gzip -c > "$partial"
test -s "$partial"
gzip -t "$partial"
chmod 0600 "$partial"
mv "$partial" "$target"
partial=
echo "Created $target"

# Only remove regular files with exact Poly2 backup names, oldest first.
shopt -s nullglob
backups=()
for file in "$backup_dir"/poly2-????????T??????Z.sql.gz; do
    [[ -f $file && ! -L $file && ${file##*/} =~ ^poly2-[0-9]{8}T[0-9]{6}Z\.sql\.gz$ ]] || continue
    backups+=("$file")
done
if (( ${#backups[@]} > keep )); then
    count=$((${#backups[@]} - keep))
    for ((i = 0; i < count; i++)); do
        unlink "${backups[i]}"
    done
fi
