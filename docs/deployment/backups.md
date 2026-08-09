# Encrypted backups

Yinshi snapshots the control database and every tenant database through SQLite's backup API. It verifies each snapshot, packs the verified files into a tar archive, and encrypts the archive with AES-256-GCM before writing the final artifact. Plaintext staging files are removed whether the backup succeeds or fails.

## Configuration

Set these values in the backend environment:

```dotenv
BACKUP_DIR=/var/lib/yinshi/backups
BACKUP_ENCRYPTION_KEY=<64 hexadecimal characters>
BACKUP_RETENTION_DAYS=30
BACKUP_UPLOAD_COMMAND=
```

`BACKUP_ENCRYPTION_KEY` must decode to 32 bytes. Store it separately from the backup artifacts. Losing this key makes every retained backup unreadable. Changing it does not re-encrypt existing artifacts, so retain old keys until their backups expire.

`BACKUP_UPLOAD_COMMAND` is optional. When set, `backend/scripts/backup.sh` invokes the command with the encrypted artifact path as its only argument. The command must return a nonzero status when upload or remote verification fails.

## Create a backup

The scheduled job should execute:

```bash
backend/scripts/backup.sh
```

A successful run prints the path to one `yinshi-<timestamp>.tar.gz.enc` artifact. The script then runs the optional upload command and removes local artifacts older than `BACKUP_RETENTION_DAYS`. The backup directory and generated artifacts use owner-only permissions.

## Restore into an isolated directory

Never extract a backup over the live data directory. Restore into a new owner-only directory:

```bash
python -m yinshi.backup restore \
  /var/lib/yinshi/backups/yinshi-<timestamp>.tar.gz.enc \
  /var/lib/yinshi/restore-drill
```

The restore command authenticates the encrypted artifact before extraction, rejects links and unsafe archive paths, limits archive member count, and validates every restored SQLite database. It refuses a nonempty destination.

After validation, stop the backend, preserve the current data directory, move the restored database files into their expected control and tenant paths, and start the backend with the same SQLCipher and key-encryption settings used when the backup was created. Keep the preserved directory until application health and tenant access are confirmed.

## Restore drill

Run a restore drill after changing database encryption, backup keys, SQLite versions, or storage providers, and at least once per release cycle. The artifact must decrypt with the separately stored backup key. Its manifest must match the restored control database and each tenant database, and every database must pass SQLite integrity checks.

Confirm that plaintext SQLite cannot open encrypted tenant databases. Start a test backend against the restored files and inspect representative user records, repository state, workspace state, and saved sessions. After the drill, verify that no plaintext staging archive remains.

Record the artifact timestamp and application revision. Include the restored database count, validation outcome, responsible operator, and cleanup confirmation.