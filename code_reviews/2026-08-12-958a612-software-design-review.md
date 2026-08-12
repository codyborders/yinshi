# Software Design Review: `958a612`

| Field | Value |
| --- | --- |
| Date | 2026-08-12 |
| Commit | `958a612030b0236c0e4667f49c02ce1ac2f2e92b` |
| Comparison | First parent through `958a612` |
| Scope | Managed backup lifecycle and supporting APIs |
| Lens | Principles from *A Philosophy of Software Design* |
| Outcome | All confirmed material defects were fixed |

## Summary

The commit introduced durable managed backup and restore. Its structure is sound. The catalog owns persistent state. The manager coordinates provider work. The relay controls live transport.

The review found three concrete defects at recovery boundaries. Each correction moved recovery knowledge into its owning module. No broad restructuring was needed.

## Confirmed findings and corrections

### High: Create operations omitted durable source identity

`backend/src/yinshi/services/managed_backups.py` created backup operations without `source_runner_id` or `source_sprite_id`. Relay recovery therefore could not reconstruct a maintenance fence after process restart.

Correction: `start_managed_backup_creation()` now copies both identities from the exact ready runtime into the operation transaction. A database-backed relay restart test confirms that a new broker rejects transfers and sends the exact job-bound quiesce frame.

Design effect: recovery no longer depends on live manager memory. The catalog now contains the identity required to interpret its own durable operation.

### High: Failed restore cleanup retained unusable candidate identity

`backend/src/yinshi/services/managed_backup_manager.py` deleted a failed candidate but retained its runner and Sprite IDs. A later claim could attempt to reuse a deleted candidate.

Correction: `clear_managed_backup_candidate()` clears the exact leased candidate and returns the restore phase to `claimed`. The manager calls it only after provider deletion succeeds. If deletion fails, durable identity remains available for another cleanup attempt.

Design effect: provider deletion and catalog transition now have one clear ordering rule. The retry path does not need to infer whether a named candidate still exists.

### Medium: Durable catalog conflicts escaped as HTTP 500 responses

`backend/src/yinshi/api/managed_runtime.py` handled `ValueError` but not `ManagedBackupConflictError`. Expected concurrent-operation conflicts could reach the generic server-error path.

Correction: all managed backup mutation routes map both state exceptions to the existing safe HTTP 409 response. Provider text remains private.

Design effect: the API masks a lower-level persistence exception behind one stable client contract.

## Additional recovery correction

Recovered relay maintenance initially blocked transfers but treated quiescence as already acknowledged. Coordination could resume before the reconnected runner confirmed that services and writers had stopped.

`RunnerRelayBroker` now creates a pending acknowledgment future when it reconstructs maintenance from the catalog. Same-job `quiesce_runner()` calls wait for the exact runner acknowledgment. Duplicate reconnect acknowledgments are safe. Failed catalog lookups do not publish dead connections. The relay sends `welcome` before recovered maintenance control.

The migration fills missing source identities for matching running operations. This lets relay fencing survive upgrades from the earlier schema.

## Design assessment

### Module depth

`ManagedBackupManager` presents a small queue-and-reconcile interface. It hides provider coordination, lease handling, encrypted jobs, and crash recovery.

### Information hiding

The catalog owns durable operation state and lease checks. The relay reads only the operation needed to restore its fence. API routes hide storage metadata.

### Error handling

Expected state conflicts now have one public representation. Candidate deletion is idempotent because a missing Sprite counts as deleted. Unexpected provider failures leave enough durable state for retry.

### Remaining risks

Public managed launch must remain disabled. Uncertain immutable uploads still require durable exact-version reconciliation. Live disaster-recovery drills still need independent versioned object storage. Sprite storage-encryption verification also remains incomplete.

These risks are outside the three reviewed defects. They remain launch gates rather than regressions introduced by these corrections.

## Validation

Backend validation passed 1,238 tests after the final reviewer corrections. Black and isort checks passed. Flake8 and strict mypy also passed.

Frontend Vitest passed. Type checking and the production build passed. Sidecar Node tests passed. Desktop tests passed with type checking and build checks.

`pip-audit` reported no known vulnerabilities. `git diff --check` passed.
