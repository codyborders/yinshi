# Managed Runtime Recovery Runbook

## Scope and beta status

Use this runbook for managed backup, restore, deletion, lease, storage, or Sprite reconciliation incidents. Managed beta can operate while recovery, monitoring, and guarded cleanup remain healthy.

## Ownership and severity

DigitalOcean sends operational alerts to the account email. The available operator handles recovery and escalation.

Treat data loss risk, mixed restore roots, authority conflicts, failed source-loss recovery, or unbounded orphan deletion as critical. Treat stale backups, stuck operations, expired leases, restore failures, deletion failures, storage preflight failures, and reconciliation failures as high severity. Delayed cleanup is medium severity only when one active runner is verified and an immutable backup is recoverable.

## Immediate containment

Pause managed provisioning when continued provisioning could increase incident impact. Stop the managed backup manager and Sprite reconciler before manual SQL or provider changes. Preserve the control database, structured logs, and sanitized checker output.

Before deleting any source or candidate, confirm its SQL identity against the generation and job. Confirm operation phase separately. Do not expose user IDs, job IDs, Sprite names, object keys, paths, tokens, or provider response bodies in tickets.

## Diagnosis

Run the operational checker against a read-only control database:

```bash
python -m yinshi.managed_operations_check \
  --control-db /var/lib/yinshi/control.db \
  --backup-stale-seconds 86400 \
  --operation-stuck-seconds 3600
```

Exit code `0` means no critical finding. Exit code `2` means one or more alert classes need action. Output contains counts and oldest ages only.

The checker reports `managed_backup_stale`, `managed_operation_stuck`, `managed_operation_lease_expired`, `managed_restore_failed`, and `managed_deletion_failed`.

DigitalOcean checks `GET /health/managed` from three regions. The endpoint returns `200 {"status":"ok"}` when no managed finding exists. It returns `503 {"status":"critical"}` for a managed finding or status-read failure. It never returns identifiers or provider details.

The `yinshi-managed-operational-critical` alert sends email after two minutes of global failure. DigitalOcean also sends recovery notification. A successful Sprite reconciliation or storage preflight clears its corresponding persistent failure. The ordinary `GET /health` process check remains independent.

## State interpretation

The `managed_runtimes` row identifies current runner and Sprite. It also records lifecycle and generation. Running restore fields identify the pre-activation source plus any replacement candidate. A deterministic candidate can exist before SQL records its provider identity.

Phase `activated` means candidate authority is active. Recovery must move forward from this point. A `managed_retired` runner must remain revoked after activation.

## Pre-activation recovery

First, verify lease expiry or exact worker ownership. Confirm that runtime still points to the source generation. Revoke candidate registration and runner authority. Delete the exact candidate Sprite, treating provider `404` as completed deletion.

Restart source services if they were quiesced. Release source maintenance for the exact job. Check operation fencing again before retry. Rollback is allowed only before activation.

## Post-activation recovery

Never reactivate the retired source. Keep the candidate as active `managed` runner. Confirm that source runner remains `managed_retired` with revoked authority.

Delete the exact old source Sprite, treating provider `404` as completed deletion. Remove candidate maintenance files for job and archive. Remove result files separately. Complete the operation with exact job, generation, current lease owner, lease token, and lease expiry. Keep phase `activated` until exact source deletion finishes.

After activation, recovery always moves forward.

## Source-loss restore

Select the exact immutable object version from a ready archive record. Confirm its checksum and wrapped key metadata. Confirm owner digest and runtime generation before provisioning the deterministic replacement Sprite.

Upload the exact encrypted archive version with its sealed restore job. Wait for the durable guest result. Verify both restored roots, SQLite integrity, and content digests. Confirm replacement identity and sole authority before atomic activation. Then follow post-activation recovery.

Do not use Fly checkpoints as the recovery source.

## Cleanup

Inspect cleanup candidates without changing provider or control-plane state:

```bash
python -m yinshi.managed_sprite_cleanup
```

The command prints sanitized counts only. Review `eligible`, `retained`, and `deferred` before execution. A failed inventory or provider read returns a fixed failure status without exposing provider details.

Delete only old, deployment-owned Sprites that remain unreferenced after the final ownership check:

```bash
python -m yinshi.managed_sprite_cleanup \
  --execute \
  --confirm-delete-unreferenced-managed-sprites
```

Both flags are mandatory. The command completes provider inventory before any mutation. It never treats a matching name prefix as ownership. Retry the same command after an interrupted deletion. Registry cleanup for an already absent Sprite requires a direct provider absence check.

Before closure, confirm that failed candidate authority is revoked and retired source authority stays revoked. Check selected object versions against retention policy. Confirm that no multipart upload remains. Guest maintenance files and local encrypted staging directories must be absent.

## Incident records

Record commit SHA and UTC times. Record alert counts, oldest ages, recovery phase, immutable archive ID, object version, and cleanup outcome. Store sensitive identifiers only in the protected incident system. Never put secrets or tenant paths in workflow artifacts.

## Exit and escalation

Close the incident only after data checks pass. One runner must hold authority. Provider cleanup must be complete, backup recovery must remain available, and monitoring must resolve.

Escalate immediately when provider inventory is incomplete, storage encryption facts are unavailable, authority cannot be established, or cleanup cannot be verified. Resume affected managed operations only after recovery and monitoring checks pass.
