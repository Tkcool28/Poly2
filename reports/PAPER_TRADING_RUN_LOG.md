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
