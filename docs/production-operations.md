# Poly2 production operations

The production checkout is `/opt/poly2`. Host Caddy serves
`https://2tb-dashboard.duckdns.org/` and forwards authenticated Poly2 traffic
to Compose nginx at `127.0.0.1:8790`. Keep the existing TT EDGE Caddy routes
ahead of the Poly2 fallback. The production `.env` stays root-only and must
retain `POLYCOPY_PAPER_MODE=true`, `POLYCOPY_ALLOW_LIVE_TRADING=false`, and
`POLYCOPY_ORDER_KILL_SWITCH=true` unless the operator explicitly changes them.
These instructions do not enable paper execution or live trading.

## Deployment, restart, reboot, rollback

Before the first update, take and verify a backup. Review the incoming commit,
including migrations, before moving the checkout. From `/opt/poly2`, use:

```bash
sudo ./infra/backup-postgres.sh
sudo ./infra/verify-postgres-backup.sh /var/backups/poly2/poly2-YYYYMMDDTHHMMSSZ.sql.gz
sudo git pull --ff-only
sudo ./infra/deploy.sh
sudo docker compose ps
```

Replace the example filename with the backup just created. The deploy script
pulls/builds images, applies forward migrations, starts Compose, and checks
`/api/health` through the configured loopback listener. Validate the
authenticated public endpoints listed in the README after deployment. To
restart stopped services without removing volumes, use `sudo docker compose up -d`.
To restart a specific running container, use `sudo docker compose restart SERVICE`.
Do not use `docker compose down -v`: the `postgres_data` Docker volume contains
the production database. Find its actual host path with
`sudo docker volume inspect "$(sudo docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"] + "_postgres_data")')"`.

All six long-running services use `restart: unless-stopped`. A container crash,
Docker daemon restart, or VPS reboot restarts services unless an operator
deliberately stopped them. The one-shot `migrate` service remains
`restart: "no"`. The optional systemd Compose unit starts the stack after
Docker on boot. Its stop action uses `docker compose stop`: containers remain
present, their volumes remain mounted, and `unless-stopped` remembers the
deliberate stop. `systemctl start polycopy.service` brings them back with
`compose up -d`. The unit does not run a migration downgrade or remove volumes.

To install and enable the optional stack unit on the VPS:

```bash
sudo install -m 0644 /opt/poly2/infra/systemd/polycopy.service /etc/systemd/system/polycopy.service
sudo systemctl daemon-reload
sudo systemctl enable --now polycopy.service
```

For a code rollback, check out a reviewed earlier commit and rerun
`sudo ./infra/deploy.sh`, then validate endpoints. **Git rollback does not
downgrade PostgreSQL migrations.** Check schema compatibility first and use a
tested backup restore plan when a schema change cannot safely be rolled back.
Never restore onto the production database as part of a routine code rollback.

## Logs and health

Each long-running Poly2 service uses Docker `json-file` logging with five
files of at most 10 MB each (about 50 MB per service). This is a per-container
policy; no Docker daemon setting or unrelated container is changed. Recreate
containers with `sudo docker compose up -d` after adopting this Compose change for
the logging settings to take effect. Inspect with `sudo docker compose logs SERVICE`.

Postgres, Redis, backend, frontend, and nginx have health checks. The frontend
checks its local served root; nginx checks its local `/api/health` proxy route.
The bot has no container-local health check: its successful-cycle heartbeat is
stored in PostgreSQL, and a process-only check would miss a stuck or failing
cycle. `/api/health/deps` reports the bot heartbeat; also inspect
`sudo docker compose logs bot` if that check is stale. Docker's health status alone
does not automatically restart an unhealthy running container.

## Backups and restore drill

`infra/backup-postgres.sh` runs `pg_dump` inside the Compose Postgres container
using its configured `POSTGRES_USER` and `POSTGRES_DB`. No credential is passed
in arguments or stored in the backup scripts. It writes a gzip-compressed
logical dump atomically under `/var/backups/poly2` by default. The directory is
root-owned mode `0700`; each backup is mode `0600`. A failed dump or gzip
validation leaves no published backup. The script keeps the newest **14**
timestamped Poly2 backup files by default and only deletes older regular files
matching its own `poly2-YYYYMMDDTHHMMSSZ.sql.gz` filename pattern. Configure
`POLY2_BACKUP_DIR` and `POLY2_BACKUP_KEEP` in the backup service environment
or when invoking the script; the latter must be a positive integer. Ensure
the backup filesystem has sufficient free space and copy verified backups to
separate durable storage under the site's recovery policy.

Manual backup and verification (replace the filename with the generated one):

```bash
sudo /opt/poly2/infra/backup-postgres.sh
sudo /opt/poly2/infra/verify-postgres-backup.sh /var/backups/poly2/poly2-YYYYMMDDTHHMMSSZ.sql.gz
```

Verification checks gzip integrity, creates a uniquely named
`poly2_restore_verify_*` database, restores with `psql -v ON_ERROR_STOP=1`,
checks the restored Alembic table, then drops **only that temporary database**.
It never connects to the production database for restore. If cleanup fails,
the script exits with an error and prints the temporary database name for
manual inspection. A dump is not considered restore-verified until this drill
succeeds. The daily timer creates backups; run the restore drill separately,
for example after each deployment and periodically thereafter.

Install the backup timer and run its first backup on the VPS:

```bash
sudo chmod 0750 /opt/poly2/infra/backup-postgres.sh /opt/poly2/infra/verify-postgres-backup.sh
sudo install -m 0644 /opt/poly2/infra/systemd/poly2-backup.service /etc/systemd/system/poly2-backup.service
sudo install -m 0644 /opt/poly2/infra/systemd/poly2-backup.timer /etc/systemd/system/poly2-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now poly2-backup.timer
sudo systemctl start poly2-backup.service
sudo systemctl status poly2-backup.service poly2-backup.timer
```

The root-run timer fires daily at **04:15 UTC** and has `Persistent=true`, so
systemd runs a missed backup after the host comes back. It does not install or
enable itself from CI. A stopped Postgres container makes backup fail visibly;
it does not trigger a production restart. Review backup logs with
`journalctl -u poly2-backup.service` and inspect `/var/backups/poly2`.
