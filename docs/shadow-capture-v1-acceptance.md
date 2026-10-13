# Prospective Shadow capture V1 acceptance matrix

Status: correction pass implemented and validated; **fresh independent re-review pending**.
The preceding independent review found four blocking defects. This document does
not replace that FAIL verdict or claim a reviewer PASS. No publication, production
installation, migration, observation window or launch was performed.

Base/HEAD: `bd61efc90a4ff8bfc17b8a31abc4a40dd655f81f`.
Branch: `feat/shadow-prospective-capture-v1` (unstaged).
Historical test consumer (not a runtime revision requirement): `fc8eb3b755c22695179c75968a84721be7590232`.

## Blocking-review correction proofs

Native regressions were written and executed before the four production fixes:
`correction-red.xml` records **11 failures, zero errors/skips**. The same eleven
cases then passed (`correction-green.xml`). Further deterministic race tests and
custody/rollback compatibility cases extend the acceptance boundary; there are
**35 additional collected cases** compared with the preceding 350-test suite.

| Finding | Actual native tests / evidence |
|---|---|
| P0 observation SQL abort silently loses source Trade | `test_capture_observation_read_sql_failure_preserves_source[wallets/markets/trades]`: inject actual PostgreSQL `SELECT 1/0` at each read seam; ordinary commit returns and a separately reopened source session proves one durable Trade. Each observation read is isolated by a connection SAVEPOINT; comparison fails. `test_capture_source_flush_error_not_swallowed` preserves real source errors. |
| P1 cleanup rollback falsely resolves uncertain COMMIT | `test_capture_commit_response_failure_cleanup_is_not_rollback_proof[True/False]`: delegate real dialect commit before raising, or raise before delegating; cleanup rollback leaves zero ROLLED_BACK resolutions. Actual durable Trade/witness counts are respectively 1/1 and 0/0. Witness recovery resolves only the committed case; the other remains outstanding and cannot fence. `test_capture_native_source_rollback` and `test_capture_commit_then_new_transaction_real_rollback` retain genuine ordinary rollback proof. |
| P1 pre-Fence startup failure can be corrected into certification | `test_capture_pre_fence_failure_correction_cannot_certify` covers each required missing input plus wrong pin/worker/run/generation and missing key/authority paths. `test_capture_pre_fence_custody_failure_restore_cannot_certify` covers malformed/wrong authority, installed component mismatch, wrong key and key mode. Close an enrolled backend, fail before Fence construction, restore correct inputs: retained generation remains denied and controller fence/freeze/handoff reject. Ordinary source writes still persist. `test_capture_wrong_spool_cannot_deny_unrelated_custody` proves a copied public target cannot redirect denial to another spool. Native factories use distinct backend/bot health custody paths. |
| P1 existing observed sessions mutate terminal ARCHIVE | `test_capture_archive_retires_existing_idle_session[False/True]`: existing and new sessions persist source Trades before/after worker control observes terminal state; exact retained spool file bytes do not change. `test_capture_archive_races_existing_session_admission` puts ARCHIVE between the outer check and locked admission. `test_capture_archive_control_during_source_commit_dispatch` executes worker control while source autoflush is inside before_commit event dispatch; safe deferred removal avoids deque mutation. `test_capture_observation_failure_racing_archive_cannot_mutate_terminal` injects real SQL failure after a competing handle archives: no post-terminal ERROR/sidecar/local error. This extra race first failed independently (`correction-terminal-race-red.log`) before the guarded invalidation fix. `test_capture_prefence_existing_session_blocks_archive` proves outstanding old admissions are not retired to fabricate closure. |

During correction validation, the first extended run exposed a listener-removal
race (`RuntimeError: deque mutated during iteration`). The dispatch guard was
extended across source autoflush, and both deterministic native control races plus
all complete final suites were rerun. Initial failures remain in scratch logs;
no assertion was weakened to hide them.

## Remaining acceptance-to-test map

| Boundary | Actual named Poly2 tests |
|---|---|
| disabled default, source availability, no business/control mutation, shared factory cache ordering | `test_capture_disabled_zero_work`, `test_capture_missing_authority_source_remains_available`, `test_capture_safety_flags_default_and_failure`, `test_capture_shared_factory_installed_before_cache`, `test_capture_config_failure_source_available` |
| actual installed identity; full, unique, valid backend/bot roster; each worker enrollment/ACK | `test_capture_actual_installed_identity`, `test_capture_invalid_roster`, `test_capture_duplicate_identity_latches_authority`, `test_capture_multi_instance_enrollment_and_each_ack`, `test_capture_missing_instance_enrollment_restart` |
| key exact mode/owner/type/symlink/matching public key | `test_capture_key_mode`, `test_capture_key_symlink`, `test_capture_key_custody_type_owner`, `test_capture_wrong_key` |
| signature/torn journal, partial append/signing failure, stale/replaced spool | `test_capture_append_failure_source_commit`, `test_capture_journal_restart_rejects`, `test_capture_stale_spool_binding_refused`, `test_capture_replaced_spool_restart_refuses` |
| native atomicity, row/outer/nested SAVEPOINTs, source rollback, witness failure | `test_capture_native_atomicity`, `test_capture_native_row_savepoint_failure`, `test_capture_native_outer_nested_rollback`, `test_capture_native_source_rollback`, `test_capture_native_witness_failure_contained` |
| process death after real source commit, restart, unknown not guessed | `test_capture_native_process_death_commit_recovery`, `test_capture_native_unknown_not_guessed`, `test_capture_native_official_cli` |
| missing/idempotent/conflicting ACK, inclusive CLOSE endpoint, actual controller lifecycle | `test_capture_ack_idempotence_conflict_missing`, `test_capture_close_exact_inclusive_endpoint`, `test_capture_controller_real_lifecycle` |
| read-only repeatable-read native FREEZE, checkpoint/source fact equality | `test_capture_native_reader_denies_write`, `test_capture_checkpoint_mismatch`, `test_capture_native_exact_population_endpoint` |
| original initial decision + unique audit, pending, rollback, late legitimate decision, audit failures | `test_capture_native_initial_decision_audit`, `test_capture_native_audit_denials` |
| fixed IDs and late source exclusion; exact E/E+1 microsecond; persisted Numeric scale/rounding and offset clocks | `test_capture_native_late_source_fixed_ids`, `test_capture_native_exact_population_endpoint`, `test_capture_native_precision` |
| additive real Alembic fresh schema, populated retained witness downgrade/re-upgrade, legacy missing-table upgrade | `test_capture_native_migration_roundtrip`; five original PostgreSQL migration regressions also executed in the final full suite |
| real canonical writer → witness → restart → fence → freeze → original decision/audit → drain → ARCHIVE → exact Shadow file seal/validator/official CLI, missing-ARCHIVE prefix denial | `test_capture_native_official_cli` + `shadow_capture_e2e.mts` |
| scientific/trading business bytes unchanged | `test_capture_trading_business_bytes_unchanged` (11 exact base-byte checks); separate 193-case ingestion/approval/scoring/sizing/bookwalk/fees/accounting/settlement/paper lifecycle regression run |

## Measured final gates

All logs/XML are outside Git under `/root/.hermes/cache/scratch/poly2-capture-v1/`.
Final focused/native/full/Ruff gates exercised the final code; subsequent changes
only document results. Earlier trading/frontend/compose checks cover their unchanged
business/build surfaces; the complete final backend suite also re-executes the
trading tests.

| Gate | Measured result / retained evidence |
|---|---|
| focused capture + native migration | **112 passed, zero skips/errors/failures**; `correction-focused-final.xml` / `.log` |
| native-only capture | **96 passed, zero skips/errors/failures**; `correction-native-final.xml` / `.log` |
| complete backend including original PostgreSQL migrations | **385 passed, zero skips/errors/failures**, 13 warnings; **87.90%** source coverage (2426/2760 lines, required ≥80%); `correction-full-final.xml` / `.log`, `correction-coverage-final.xml` |
| trading/ingestion/approval/scoring/accounting regressions | **193 passed, zero skips/errors/failures**; `correction-trading.xml` / `.log` |
| exact consumer authority tests | **49 passed** via `npm test -- tests/poly2-production-authority.test.ts` in the clean exact Shadow checkout |
| actual crossrepo canonical native handoff / official CLI | exit **0**, validated **1 Poly2 row**, `poly2Only=1`, consumer SHA exactly `fc8eb3b755c22695179c75968a84721be7590232`; printed `OFFICIAL_POLY2_NATIVE` in the final native log; no synthetic override |
| full backend Ruff | **All checks passed** for `backend/src backend/tests`; `correction-ruff-final.log` |
| frontend | existing installed dependencies, `npm run build` succeeds; generated untracked `frontend/tsconfig.tsbuildinfo` removed by exact owned path |
| compose + deployment shell syntax | compose `config --quiet` succeeds with disposable interpolation values (no service started); `bash -n` succeeds for `infra/deploy.sh`, `infra/backup-postgres.sh`, `infra/verify-postgres-backup.sh` |
| diff/publication boundary | `git diff --check` clean, index empty, HEAD/base unchanged; no stage/commit/push/PR/deploy |

The first full rerun had five legacy migration skips because their dedicated DSN
was unset. The final full rerun uses the previously created owned
`poly2_capture_suite_owned` database, whose task provenance was verified, and has
zero skips. A proposed separate create/drop-database wrapper requested tool
approval and was not executed; it was unnecessary after discovering the existing
owned suite database. No substitute/fabricated database results were used.

## Fixture and frozen protocol

Reused only the verified task-owned fixture:
`fcc71ee25bc6f65d6809546317a5d39a15543305be9996565f8682fe9c7c7db7`,
name `poly2-capture-v1-owned-native`, label `hermes.task=poly2-capture-v1`,
network `none`, tmpfs PostgreSQL data/socket, no mounts or published ports,
`listen_addresses` empty. Socket: `/proc/2160204/root/var/run/postgresql`.
The fixture remains running for the parent's independent re-review; cleanup is
intentionally deferred. Test-owned migration databases were removed by their
existing fixture cleanup; the parent-owned suite database was retained.

All eight vendored Shadow scripts remain byte-identical to the exact consumer
checkout and their manifest; no protocol amendment was made.

- Unchanged protocol digest: `ff58bd29b67c20d249d7b4ef9ad130fcd33a14aa975c122d8b9c75ff5f742360`.
- Revised installed component digest: `37f29699bd8deaf729957f3b7bfc53ae467e706c46763cfcaa662b5d0cf5b2db`.

Any prior independent approval for old component bytes is invalid for these new
bytes; a future installation requires separately approved identities. The final
unstaged inventory and individual SHA256 values are retained externally in
`correction-final-inventory.json`; fresh independent re-review remains required
before publication or any installation readiness claim.
