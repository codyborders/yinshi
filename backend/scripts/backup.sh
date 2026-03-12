#!/bin/bash
# Cron-safe backup script for Yinshi multi-tenant databases.
# Backs up the control DB and all per-user DBs using sqlite3 .backup
# (WAL-safe, no locking issues).
#
# Usage:
#   ./backup.sh                    # Local backup only
#   ./backup.sh --upload           # Also upload to GCS
#
# Cron example (every 6 hours):
#   0 */6 * * * /opt/yinshi/backend/scripts/backup.sh --upload >> /var/log/yinshi-backup.log 2>&1

set -euo pipefail

DATA_DIR="${YINSHI_DATA_DIR:-/var/lib/yinshi}"
BACKUP_DIR="${YINSHI_BACKUP_DIR:-/var/lib/yinshi/backups}"
GCS_BUCKET="${YINSHI_GCS_BUCKET:-gs://yinshi-backups/daily}"
RETENTION_DAYS=30

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
STAGING_DIR="$BACKUP_DIR/staging-$TIMESTAMP"

mkdir -p "$STAGING_DIR"

echo "[$TIMESTAMP] Starting backup..."

# Backup control DB
CONTROL_DB="$DATA_DIR/control.db"
if [ -f "$CONTROL_DB" ]; then
    echo "  Backing up control.db"
    sqlite3 "$CONTROL_DB" ".backup '$STAGING_DIR/control.db'"
fi

# Backup all user DBs
USER_DIR="$DATA_DIR/users"
if [ -d "$USER_DIR" ]; then
    BACKED_UP=0
    for user_db in "$USER_DIR"/*/*/yinshi.db; do
        if [ -f "$user_db" ]; then
            # Preserve directory structure: users/a1/a1b2c3.../yinshi.db
            REL_PATH="${user_db#$DATA_DIR/}"
            DEST_DIR="$STAGING_DIR/$(dirname "$REL_PATH")"
            mkdir -p "$DEST_DIR"
            sqlite3 "$user_db" ".backup '$DEST_DIR/yinshi.db'"
            BACKED_UP=$((BACKED_UP + 1))
        fi
    done
    echo "  Backed up $BACKED_UP user database(s)"
fi

# Create tarball
TARBALL="$BACKUP_DIR/yinshi-$TIMESTAMP.tar.gz"
tar czf "$TARBALL" -C "$STAGING_DIR" .
rm -rf "$STAGING_DIR"
echo "  Created $TARBALL"

# Upload to GCS if requested
if [ "${1:-}" = "--upload" ]; then
    if command -v rclone &> /dev/null; then
        echo "  Uploading to $GCS_BUCKET"
        rclone copy "$TARBALL" "$GCS_BUCKET/"
    elif command -v gsutil &> /dev/null; then
        echo "  Uploading to $GCS_BUCKET"
        gsutil cp "$TARBALL" "$GCS_BUCKET/"
    else
        echo "  WARNING: Neither rclone nor gsutil found, skipping upload"
    fi
fi

# Clean up old backups
find "$BACKUP_DIR" -name "yinshi-*.tar.gz" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null || true

echo "[$TIMESTAMP] Backup complete: $TARBALL"
