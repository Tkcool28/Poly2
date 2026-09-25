# Poly2 paper trading experiment ledger

Append new dated entries at the end. PostgreSQL remains the system of record;
this file records the operator's interpretation and deployment context. Do not
write database passwords, API tokens, credentials, or private keys here.

The proposed PRs #16–#19 are **not deployed or merged** as of this initial
entry. A merged/deployed change needs a new entry, including its actual SHA,
migrations, CI, observed health, and safety posture. Do not prefill production
results from mock tests.

## 2026-09-24 — Baseline before paper readiness milestone

- Production commit SHA: `a3dd0469b9e3c1be2185f05f50a96b0ba21ecaac` (merged PR #15, reported deployed).
- Test/run identifier: `baseline-pr15-candidate-wallet-4`.
- Paper configuration: paper mode `true`; live trading `false`; kill switch `true`; no Polymarket private key configured. Execution sizing and fee settings: not captured in the supplied production evidence.
- Approved wallets: not provided in the supplied evidence. Wallet `0x6d3c5bd13984b2de47c3a88ddc455309aab3d294` was a **candidate**, not approved by this entry.
- Health: production deployment reported healthy; no independent watchdog measurement in this entry.
- Source observation: candidate bootstrap fetched 1,000 historical rows; inserted 477, skipped 523 canonical duplicates, quarantined 0. Local count reached 1,000; oldest observed `2026-09-20 15:33:49 UTC` and newest `2026-09-24 19:13:36 UTC`.
- Signals, fills, partials, misses, stale signals, settlements of paper positions, realized paper P&L: not measured in the supplied run.
- API/rate limits: bootstrap used 10 pages, 50 logical requests, 20 cumulative settlement-market checks; configured limit reached. Gamma HTTP 429 observed during the broader production audit; count/timestamps not supplied.
- Data gap: 30-day account-age evidence was unavailable within the configured 1,000-row bound. This establishes bounded local evidence only, not the actual age of the source wallet.
- Operator actions: candidate added through supported intake; no approval, execution switch change, or production DB edit recorded here.
- Conclusion: scorer correctly retained `insufficient_history` (`account_age_days 4 < 30`). The data transport and paper-flow integration gaps motivate PRs #16–#19.
- Next action: independently review each PR, merge in order only after review, deploy migrations while keeping the kill switch ON, then append a deployment entry with the actual deployed SHA and post-deploy health.

## Entry template — append for each deployment or controlled run

- Timestamp (UTC):
- Merged PR(s) and deployed SHA:
- Migration status and CI/full-suite evidence:
- Run identifier and approved wallets:
- Relevant configuration: paper mode, live permission, kill switch, review delay, max signal age, fixed order size, fee, exposure caps (no secrets).
- System health: API/deps, bot success heartbeat age, watchdog status, data/API errors and rate limits.
- Source trades observed and detection lag distribution:
- Signals generated and eligible; kill-switch deferrals:
- Fills, partials, misses by reason, stale signals:
- Book depth, slippage, fees and copy rate:
- Settlements; SELL/settlement/total realized P&L; open and per-market exposure:
- Source-side outcome comparison (evidence and limits; do not fabricate unavailable P&L):
- Data gaps, operator actions and important anomalies:
- Conclusion and next action:

## 2026-09-25 00:09 UTC — PRs #16–#19 production deployment/readiness inspection

- Run identifier: `pr16-19-paper-readiness-deploy-20260925`; PostgreSQL is the system of record. Starting production `main` was `a3dd0469b9e3c1be2185f05f50a96b0ba21ecaac`; fetched and fast-forwarded to `228da83c34f7b405f6ccfeecc54949e9ba662936`. The four specified merge commits (#16 `e115ac8`, #17 `6aadd6c`, #18 `9d6e4b1`, #19 `228da83`) were verified as ancestors of `origin/main`.
- Backup `/var/backups/poly2/poly2-20260924T235140Z.sql.gz` passed a temporary-database restore drill. Repository deployment applied Alembic `0007 -> 0008 -> 0009 -> 0010`; production revision read back as `0010`. User-supplied merge history reports passing backend/frontend/Compose CI on the stacked PRs; independent on-host isolated backend validation: 57 targeted tests passed, full suite 214 passed / 3 skipped with 87.03% coverage. The full-suite warning was a Starlette/httpx deprecation warning, not a failed test.
- Safety remained paper mode `true`, live trading `false`, order kill switch `true`, private key not configured. Review delay 30 seconds; source-to-execution maximum age 300 seconds; fixed order size $10; paper fee rate 0; market/global exposure caps $100/$500. No execution switch or approval state was changed.
- At inspection, PostgreSQL, Redis, backend, frontend, nginx and independent watchdog were Docker-healthy; bot was running with fresh completed-cycle heartbeat. At 00:08:57 UTC, latest `bot_alive` was 00:08:48 and `bot_success` 00:08:49; no `bot_failure` row was returned. Watchdog check-once passed its 600-second success-freshness policy. Local dashboard/API returned 200, public Poly2 required auth (401), and the three checked TT EDGE aliases returned 200.
- Deployment anomaly: the documented deploy script completed migrations/startup but exited nonzero after its health timeout. Retained nginx held stale backend/frontend upstream addresses and returned 502; a restart of **only nginx** restored both dashboard and API to 200 and nginx health. No repository code or Caddy route was changed.
- Autonomous candidate scoring logged an attempt at 2026-09-24 23:53:38 UTC for three candidates: one `discovered`, two `insufficient_history`; configured cadence is hourly. Candidate #4 (`0x6d3c5bd13984b2de47c3a88ddc455309aab3d294`) has persisted resumable bootstrap `in_progress`, 35 pages/3,500 fetched, 2,977 inserted, 523 duplicates, 0 quarantined, offset 3,500, 75 logical requests, oldest observed 2026-09-12. Its last persisted score still predates deployment and says `account_age_days 4 < 30`; do not treat that as a new verdict on the deeper in-progress snapshot. Another candidate has 0 trades; no maturity gate was weakened or candidate approved.
- Approved wallet #2 `0x82cf2b31d18fca19830e216b98cffa5dbc6c0998`: 490 observed trades; approved_at 2026-09-24 15:25:49 UTC; latest observed source trade 2026-09-19 20:10:32 UTC. Latest persisted catch-up state: `overlap_found`, incomplete `false`, cursor 0, no outstanding boundary. A recent latest-page poll fetched 490, inserted 0, skipped 490 canonical duplicates, quarantined 0. No production >500-row gap occurred to prove multi-cycle recovery live; the isolated recovery tests passed.
- Settlement scheduler observation at 00:08:57 UTC: 1,606 unresolved markets, 1,476 due, 160 checked within ten minutes, next due 00:08:39 UTC. Bot limits checks to 10 per cycle; no `api_throttles` Gamma row and no post-deploy 429 or settlement-check failure was seen in inspected logs. Observed cycles reported 0 new settlements and 0 settlement errors; ambiguous Gamma outcomes remained unsettled. Historical 429s before deployment do not establish a current cooldown.
- Pending signals were 0 before deployment and 0 after; stale pending 0 at both observations. No source trade was eligible after the approved_at boundary. Evidence API: 490 source trades observed, eligible source trades 0, generated signals 0, copied/partial/missed trades 0, stale misses 0, kill-switch deferrals 0, open positions/exposure 0, settled copied positions 0, and copied realized P&L $0. Detection lag, slippage, book depth, fees actually charged and copy rate cannot be measured without signals/fills. Comparable source-wallet outcome P&L is unavailable, not inferred. No order or simulated fill was caused by deployment.
- Readiness verdict: `NOT_READY_FOR_CONTROLLED_PAPER_SWITCH_AUTHORIZATION`. The isolated lifecycle harness passes, but production has no post-approval eligible trade or actual signal/execution/settlement/P&L chain, and no live >500-row continuity gap or stale backlog to exercise those recovery paths. Candidate #4's deeper bootstrap remains in progress. Next action: continue read-only monitoring of the hourly candidate-scoring result, settlement/cooldown and watchdog; obtain natural production evidence of approved-wallet transport and signal freshness before a separately authorized controlled paper-switch decision. Keep the kill switch ON and live trading disabled.