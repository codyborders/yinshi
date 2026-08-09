#!/usr/bin/env bash
# Create an application-aware, AES-256-GCM-encrypted Yinshi database backup.

set -euo pipefail
umask 077

app_root="${YINSHI_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
python_bin="${YINSHI_PYTHON_BIN:-$app_root/backend/.venv/bin/python}"
backup_dir="${BACKUP_DIR:-/var/lib/yinshi/backups}"
retention_days="${BACKUP_RETENTION_DAYS:-30}"
gcs_bucket="${YINSHI_GCS_BUCKET:-}"

if [[ ! -x "$python_bin" ]]; then
  printf 'Yinshi Python interpreter is unavailable: %s\n' "$python_bin" >&2
  exit 1
fi

cd "$app_root"
archive_path="$($python_bin -m yinshi.backup)"

if [[ ! -f "$archive_path" || "$archive_path" != *.tar.gz.enc ]]; then
  printf 'Backup command did not produce an encrypted archive\n' >&2
  exit 1
fi

if [[ "${1:-}" == "--upload" ]]; then
  if [[ -z "$gcs_bucket" ]]; then
    printf 'YINSHI_GCS_BUCKET is required for --upload\n' >&2
    exit 1
  fi
  if command -v rclone >/dev/null 2>&1; then
    rclone copy "$archive_path" "$gcs_bucket/"
  elif command -v gsutil >/dev/null 2>&1; then
    gsutil cp "$archive_path" "$gcs_bucket/"
  else
    printf 'rclone or gsutil is required for --upload\n' >&2
    exit 1
  fi
fi

find "$backup_dir" -type f -name 'yinshi-*.tar.gz.enc' -mtime "+$retention_days" -delete
printf 'Encrypted backup complete: %s\n' "$archive_path"
