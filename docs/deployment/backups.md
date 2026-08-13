# Encrypted backups

Yinshi snapshots the local control database and local tenant databases through SQLite's backup API. It verifies each snapshot, packs the files into a tar archive, and encrypts the archive with AES-256-GCM. Plaintext staging files are removed whether the backup succeeds or fails.

This guide covers local Podman and bring-your-own-cloud deployments with `MANAGED_RUNTIME_PROVIDER=disabled`.

## Managed Fly backups

The managed Fly control plane has no live tenant databases. Local create and restore commands reject `MANAGED_RUNTIME_PROVIDER=fly_sprites` with `Local backup commands are unavailable in managed Fly mode`.

Managed guests create encrypted archives for `/var/lib/yinshi/sqlite` and `/var/lib/yinshi/files`. Archive bytes are encrypted inside the guest before transfer. The control plane uploads only ciphertext to independent, versioned S3-compatible storage. Runner identity files remain outside the restored roots.

Configure these values for hosted Fly mode:

```dotenv
MANAGED_BACKUP_PROVIDER=aws_s3
MANAGED_BACKUP_BUCKET=yinshi-backups
MANAGED_BACKUP_ENDPOINT_URL=https://s3.us-east-1.amazonaws.com
MANAGED_BACKUP_REGION=us-east-1
MANAGED_BACKUP_ACCESS_KEY_ID=
MANAGED_BACKUP_SECRET_ACCESS_KEY=
MANAGED_BACKUP_PREFIX=yinshi-managed-v1
MANAGED_BACKUP_PART_BYTES=16777216
MANAGED_BACKUP_RETENTION_DAYS=30
```

The `aws_s3` profile requires versioning and AES256 default encryption. Startup fails closed when either setting is missing. Explicit access keys must be configured as a pair. Leave both key values empty when the control plane uses an instance role.

DigitalOcean Spaces uses this profile:

```dotenv
MANAGED_BACKUP_PROVIDER=digitalocean_spaces
MANAGED_BACKUP_ENDPOINT_URL=https://sfo3.digitaloceanspaces.com
MANAGED_BACKUP_REGION=sfo3
```

Spaces must have versioning enabled. Spaces does not expose bucket-default encryption or returned object-encryption metadata through its S3 API. Yinshi still requests `AES256` during multipart creation. It also downloads each completed immutable version and verifies its ciphertext digest before accepting the upload. Guest-side AES-256-GCM encryption remains mandatory for every provider.

The storage identity needs object upload, read, exact-version delete, version listing, multipart listing, and multipart abort permissions for the configured prefix. These permissions let the coordinator recover a lost upload response without creating another immutable version.

Each archive uses a random 32-byte key. The control plane stores only wrapped archive keys. The guest receives runner-bound sealed jobs and never receives object-store credentials.

Managed archive listing is available at `GET /api/runtime/backups`. Responses omit object keys, object versions, checksums, wrapped keys, provider identifiers, and runner identifiers.

Public managed launch remains disabled. Remaining orphan cleanup, restore crash coverage, live recovery drills, and trustworthy Sprite storage-encryption verification must finish before launch.

## Configuration

Set these values in the backend environment:

```dotenv
BACKUP_DIR=/var/lib/yinshi/backups
BACKUP_ENCRYPTION_KEY=<64 hexadecimal characters>
BACKUP_RETENTION_DAYS=30
BACKUP_UPLOAD_COMMAND=
```

`BACKUP_ENCRYPTION_KEY` must decode to 32 bytes. Store it separately from the backup artifacts. Losing this key makes every retained backup unreadable. Changing it does not re-encrypt existing artifacts. Retain old keys until their backups expire.

`BACKUP_UPLOAD_COMMAND` is optional. It must be an absolute path to a regular executable file. The file cannot be a symlink, group-writable, or world-writable. Its owner must be the service user or root.

`backend/scripts/backup.sh` opens the uploader without following symlinks. It confirms a regular file from descriptor metadata. It then checks ownership and permissions. It copies that content into an owner-only temporary directory, then runs the private copy. The encrypted artifact path is its only argument. The uploader receives only the fixed `PATH=/usr/bin:/bin` value. It does not inherit `HOME`, `TMPDIR`, backup keys, application secrets, provider tokens, or other service variables. The temporary copy is removed after success or failure. The command must return a nonzero status when upload or remote verification fails.

## Create a backup

The scheduled job should execute:

```bash
backend/scripts/backup.sh
```

A successful run prints the path to one `yinshi-<timestamp>.tar.gz.enc` artifact. The script then runs the optional upload command. It removes local artifacts older than `BACKUP_RETENTION_DAYS`. The backup directory and generated artifacts use owner-only permissions.

## Restore drill

Run a restore drill after changing database encryption, backup keys, SQLite versions, or storage providers. Also run one at least once per release cycle. Use the same application revision and encryption settings that created the backup.

The restore command replaces `CONTROL_DB_PATH` and tenant databases below `USER_DATA_DIR`. It does not accept a separate destination. Complete these steps during a maintenance window.

1. Stop the backend, job workers, scheduled tasks, and every process that can write application databases. Confirm that no process has the control or tenant databases open.

   ```bash
   sudo systemctl stop yinshi-backend yinshi-workers
   lsof "$CONTROL_DB_PATH" "$USER_DATA_DIR"/*/*/yinshi.db
   ```

   The `lsof` command must return no database users before restore. Adapt service names for the deployment platform.

2. Select the artifact and check its encrypted backup header. Record its checksum before restore.

   ```bash
   archive=/var/lib/yinshi/backups/yinshi-<timestamp>.tar.gz.enc
   test "$(dd if="$archive" bs=1 count=17 2>/dev/null)" = "YINSHI-BACKUP-V1"
   sha256sum "$archive"
   ```

   A matching header identifies the encrypted Yinshi backup format. The restore command performs AES-GCM authentication with `BACKUP_ENCRYPTION_KEY`. It makes no destination replacement when authentication or archive validation fails.

3. Restore the configured databases with explicit replacement confirmation.

   ```bash
   cd /opt/yinshi/backend
   python -m yinshi.backup restore "$archive" --confirm-replace
   ```

   The command rejects unsafe archive members, symlinked destination paths, and invalid version-one manifests. Before installation, it validates the control database, tenant databases, and the manifest tenant count. Staging and rollback files use owner-only permissions on the destination filesystems.

   Restore makes local tenant databases match the archive. It quarantines databases missing from the archive with the other rollback data. A failed installation restores them. A successful, durable installation removes their rollback copies.

4. Keep all writers stopped while checking SQLite integrity. Check the control database first. Use application database connections to check every tenant, including SQLCipher databases.

   ```bash
   sqlite3 "$CONTROL_DB_PATH" 'PRAGMA integrity_check;'

   python - <<'PY'
   from pathlib import Path

   from yinshi.db import get_control_db
   from yinshi.services.accounts import make_tenant
   from yinshi.tenant import get_user_db

   with get_control_db() as control:
       users = control.execute("SELECT id, email FROM users ORDER BY id").fetchall()

   checked = 0
   for user in users:
       tenant = make_tenant(str(user["id"]), str(user["email"]))
       if not Path(tenant.db_path).is_file():
           continue
       with get_user_db(tenant) as database:
           result = database.execute("PRAGMA integrity_check").fetchall()
       if len(result) != 1 or str(result[0][0]).lower() != "ok":
           raise SystemExit("tenant integrity check failed")
       checked += 1
   print(f"tenant databases checked: {checked}")
   PY
   ```

   The control command must print `ok`. The tenant command must finish without an error. Confirm that its count matches the backup manifest count.

5. If installation fails, expect a nonzero status. Restore should put old database files back from private rollback copies. An incomplete rollback retains owner-only `.yinshi-restore-rollback-*` directories containing recovery copies. Keep writers stopped. Confirm that old control and tenant databases remain intact before investigating. Do not restart against a partly checked restore.

6. Restart the backend before workers. Check application health, tenant access, repository state, workspace state, and saved sessions. Restart remaining writers only after these checks pass.

   ```bash
   sudo systemctl start yinshi-backend
   sudo systemctl start yinshi-workers
   ```

After a successful drill, check for restore leftovers. No `.yinshi-restore-stage-*`, `.yinshi-restore-rollback-*`, or plaintext tar files should remain. If rollback was incomplete, preserve rollback directories until recovery finishes. Record the artifact timestamp, checksum, application revision, database count, and validation result. Also record the operator and whether cleanup completed.
