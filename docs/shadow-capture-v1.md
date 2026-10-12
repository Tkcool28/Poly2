# Prospective Shadow capture V1 — operator handoff

**Repository implementation only. Installation, production migrations, enrollment,
activation, observation windows, sealing real evidence and scientific comparisons
have NOT been executed or authorized by this implementation task.** This is not a
historical reconstruction tool. Never infer capture completeness from a health
snapshot, MAX timestamp, current order status, current approval or settlement.

## Installed contract and architecture

The consumer is pinned to `fc8eb3b755c22695179c75968a84721be7590232`.
`backend/src/polycopy/_shadow_protocol/manifest.json` records exact upstream script
hashes; those scripts are unchanged. Local integration differences are confined to
`shadow_capture.py`, `shadow_observer.py`, `shadow_reader.py`, `shadow_controller.py`
and `shadow_failure.py`. The ORM adapter reads each INSERT's persisted native
Numeric(20,6) and timestamp values inside its original source transaction. It does
not change ORM inputs, source defaults or trading semantics. Source flush errors
propagate normally; only observational operations are fail-contained.
All observation-only Wallet/Market/Trade SQL reads, as well as witness INSERTs,
run within connection-level SAVEPOINTs. A failed observation SELECT is rolled back
to its own SAVEPOINT before it is contained; it cannot leave PostgreSQL's source
transaction aborted. These are not Session SAVEPOINTs that might autoflush source
work recursively.

`db.get_sessionmaker()` wraps the shared factory before publishing it, covering
backend `/scoring/run` and bot canonical tail/catchup/bootstrap ingestion, including
the bot's cached factory. Independent/manual SQL writers are outside the authorized
architecture; their participation must not be inferred from the approved roster.
Watchdog and migration processes do not enroll. Already-created factories/sessions
cannot be retroactively instrumented. All approved instances must start/enroll
before S and before any admission; a missed instance invalidates the experiment,
not source trading. Missing source witnesses remain OUTSTANDING, never rollback.

## Future installation prerequisites (separate authorization)

1. Approve the actual deployed backend and bot instance inventory, immutable source
   revision and integration/protocol bytes. Choose stable `backend:<incarnation>` /
   `bot:<incarnation>` IDs, including *every* replica. Do not regenerate IDs on
   restart or use one ID for simultaneous processes. A lifetime advisory file lock
   enforces exclusivity on the shared spool.
2. Use a source-checkout runtime with Git available and an editable installation of
   `backend[comparison]` (pinned cryptography 50.0.2, rfc8785 0.1.4). The current stock
   Docker image does **not** carry Git checkout metadata or comparison extras. Its
   default-disabled source services remain supported; merely enabling its env is
   NOT an installation procedure and will fail capture while keeping source sessions
   available. A separately reviewed source-aware image or checkout-based runtime is
   required before container activation. No Dockerfile/deployed image was changed.
3. Generate public identity evidence in the **actual** installed runtime:
   `python -c 'from polycopy.shadow_capture import identities,deployed_sha; print(deployed_sha()); print(identities())'`.
   `protocolDigest` = SHA256(RFC8785/JCS map of manifest filenames to the SHA256 of
   their installed bytes). `componentSha256` = SHA256(JCS map of these relative
   filenames to installed-byte SHA256): `shadow_capture.py`, `shadow_observer.py`,
   `shadow_reader.py`, `shadow_controller.py`, `shadow_failure.py`, `db.py`.
   The actual Git HEAD must equal the externally approved `poly2CodeSha`; duplicate
   supplied SHA environment values are never treated as deployment proof.
4. An independent operator supplies a `poly2-production-authority-v1` public JSON
   with exact runId, shadowRunId, generationId, Poly2/Shadow SHAs, protocol/component
   digests, five-wallet cohort, cohort digest, inclusive exact UTC window,
   `evidenceKind: observational`, producer Ed25519 public key, required services
   `[backend,bot]`, full expectedWorkers and independently approved installation
   attestations. Preserve the canonical JCS digest through a separate custody
   channel; never derive the configured trust pin from a producer-owned handoff.
   Approval and enrollment must precede S. The pinned consumer authority documents
   define the exact schema; no local replacement authority schema exists.
5. The external PKCS8 Ed25519 private key is a **file**, owned by the process UID,
   regular, no-follow, exact mode 0600 and matching the approved public key. Store
   authority and key outside fence/drain/handoff roots. OS ACLs must keep custody
   under the independently approved operator; path separation alone is not an ACL.
   Do not put key bytes in environment, logs, evidence, samples or Git. Controller
   access requires separately approved custody because it signs control records.
6. On the separately authorized source DB, apply additive Alembic `0014`. Exact
   reviewed table: `comparison_transaction_witness(transaction_id text PRIMARY KEY,
   binding_sha256 text NOT NULL,payload text NOT NULL,payload_sha256 text NOT NULL)`.
   No backfill, source-row update, historical rewrite or trading FK. A witness is
   committed in the actual Trade outer transaction using an isolated SAVEPOINT.
   Witness failure invalidates comparison without preventing that source commit.
   Downgrade intentionally **retains** witnesses; re-upgrade is idempotent. Rollback
   of instrumentation does not authorize deleting scientific evidence.
7. Set all documented capture env variables explicitly. `.env.example` contains
   only OFF/empty defaults. Compose fixes the backend/bot service labels, takes
   separately configured BACKEND_WORKER/BOT_WORKER, and forces watchdog/migrate OFF.
   An authorized overlay must separately arrange read-only custody mounts and
   durable shared fence storage; none is enabled by default. Fence SQLite locking
   must be supported by the filesystem, with one shared spool per generation.
   Drain/handoff must be distinct retained paths; handoff directory must not exist.
   Keep health paths distinct per service **and generation** to prevent diagnostics
   overwriting each other. Public denial-target custody descriptors are retained
   at `capture-custody.json` in the spool and beside each worker health path with
   the `.custody.json` suffix. They retain the previously verified public approval,
   independent pin and exact custody/spool paths, never the private key. They are
   denial-only target locators, not enrollment or certification authority. Health
   is never authority. No real `.env` was edited.
8. Restart/recreate all approved source instances *before* S to replace their
   factories. Check ACTIVE plus the entire durable enrolled roster. Do not toggle
   PAPER_MODE, ALLOW_LIVE_TRADING, ORDER_KILL_SWITCH or any business parameter to
   activate capture. Failure has no policy/trading-control side effects.

## Controller (under approved runtime configuration)

`python -m polycopy.shadow_controller --help` lists commands. It requires an existing
worker-armed spool, approved authority/pin, installed bytes and the external key.
It neither arms/enrolls instances nor impersonates their ACKs. PostgreSQL operations
always open a fresh AUTOCOMMIT connection with default read-only; FREEZE checks an
actual read-only REPEATABLE READ snapshot and exact witness/population equality.
Never point these commands at production without separate installation/run approval.

| Command | Effect / required follow-up |
|---|---|
| `status` | Replay authoritative state; inspect missing ACKs/outstanding IDs and errors. |
| `recover` | Resolve only actual visible committed DB witnesses, idempotently. No witness = unknown forever absent real rollback proof. |
| `close` | Signed CLOSE, strictly after inclusive E. At E is rejected. |
| `fence` | Sign FENCE only after every actual worker's generation ACK and all old transactions resolved. JSON null / exit 2 means not complete. |
| `freeze` | Signed fixed population with original clocks/IDs, actual SQL snapshot/predicate/query digests; mismatch fails. |
| `drain` | Read only the frozen IDs' original initial PaperOrder + unique DecisionLogEntry. PENDING is not NO_DECISION. Late legitimate decisions are permitted; late new source IDs are excluded. |
| `handoff` | Require complete drain, sign terminal ARCHIVE, exclusively write `binding.json`, `fence.ndjson`, `drain.ndjson` and fsync. Existing path or incomplete evidence rejects. |

Typical *future* sequence: `recover`, after E `close`, allow the independently
running 100ms worker controllers to ACK, then `recover`, `fence`, `freeze`, repeated
`drain` while genuinely pending, and once complete `handoff`. An ACK cannot resolve
an outstanding transaction. Stopping a process cannot be substituted for its ACK.
After terminal ARCHIVE each worker retires observation in both existing and new
source sessions. Observers are tracked weakly, marked retired immediately, and
listeners removed only outside SQLAlchemy event dispatch. Admission rechecks the
terminal state under the exact journal writer lock, closing the cross-process
check-to-append race. Old/new source writes continue unchanged; no scientific
mutation or denial sidecar may follow ARCHIVE. Pre-fence outstanding sessions are
not retired early to fabricate completeness: their unresolved admission still
blocks FENCE/FREEZE/ARCHIVE.

To seal a separately authorized offline handoff, use the exact consumer exports
`loadAuthority(authorityPath, independentPin)` and
`sealFencedArchiveFiles(binding, fencePath, drainPath, exclusiveArchivePath, external)`
from `src/compare/production-authority.ts` and `poly2-fenced-archive.ts`, then validate
with `validateFencedArchiveFile`. Supply public authority separately, not a trust
root taken from the archive. The official CLI takes:
`node --import tsx src/compare/cli.ts SHADOW_DIR ARCHIVE COHORTS OUT_DIR AUTHORITY PIN`.
Scientific execution needs its own approval. Never pass a synthetic override,
relabel evidence or reinterpret earlier experiments. The disposable native test
`test_capture_native_official_cli` exercises the exact handoff/seal/validator/CLI
ABI and missing-ARCHIVE denial; it is not a real comparison launch.

## Failure, restart and disabling

Preserve the spool, drain, witnesses and external authority unchanged. Exact worker
reattachment recovers committed witnesses even if its process died immediately
following DB commit. No intent/ACK/cache timestamp is source commit authority.
An acknowledged ordinary rollback can resolve an admission. Once outer COMMIT
has been attempted, a later Session cleanup rollback is **not** rollback proof:
both a lost response after server commit and a failure before server commit remain
OUTSTANDING until actual proof. Only a visible committed witness resolves the
former; missing witnesses never resolve the latter by inference.
Unknown outcomes, invalid signatures, partial/torn rows, changed generations,
missing enrollment/ACK/audit or altered checkpoints block certification.

A signed ERROR is retained where possible. `capture-failed.json` is a denial-only
sidecar when signing the ERROR itself fails: any presence, even torn/malformed,
refuses producer/controller restart. It can never authorize or replace journal
receipts. Do **not** delete it or retry the same generation. If storage cannot even
persist the denial, the process remains failed and logs an operator-quarantine
requirement; physical disk loss/ENOSPC recovery cannot be claimed as completeness.
Never repair/truncate/relabel a journal or guess a missing transaction outcome.
Repeated exact ACK is idempotent; conflicting generation is rejected.
Startup failures before Fence construction also deny a previously retained intended
generation. The denial path verifies the retained independent public approval and
the actual signed ARM, checks exact spool/custody targeting, and serializes its
sidecar with ARCHIVE's journal lock. A bad pin, missing input, wrong key/authority
or identity cannot be repaired into a certified run by simply restarting with
corrected inputs. A copied target descriptor or a mistaken unrelated spool path
cannot redirect this denial. If no retained custody target can be identified
safely, preserve the failed installation for operator quarantine; never guess an
unrelated target or treat a diagnostic health file as authorization.

For an authorized disable/rollback: turn capture OFF and restart affected source
instances to discard cached observed factories. Leave all retained evidence and
witnesses inert; don't clear scientific failures or delete authority. Do not modify
trading flags, policy, timers or production data as part of this repository task.
The independently approved scientific window must be abandoned/re-approved under
new identities if an unrecoverable capture gap occurred.

See [measured acceptance](shadow-capture-v1-acceptance.md) for the exact tested scope,
remaining installation prerequisites and independent-review barrier.
