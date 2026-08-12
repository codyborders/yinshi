#!/usr/bin/env bash
# Create an application-aware, AES-256-GCM-encrypted Yinshi database backup.

set -euo pipefail
umask 077

app_root="${YINSHI_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
python_bin="${YINSHI_PYTHON_BIN:-$app_root/backend/.venv/bin/python}"
backup_dir="${BACKUP_DIR:-/var/lib/yinshi/backups}"
retention_days="${BACKUP_RETENTION_DAYS:-30}"
upload_command="${BACKUP_UPLOAD_COMMAND:-}"
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

if [[ -n "$upload_command" ]]; then
  if [[ "$upload_command" != /* ]]; then
    printf 'BACKUP_UPLOAD_COMMAND must be a trusted executable file\n' >&2
    exit 1
  fi
  upload_private_path="$("$python_bin" -I - "$upload_command" <<'PY'
import os
import shutil
import stat
import sys
import tempfile

source_path = sys.argv[1]
source_fd = -1
private_directory = ""
try:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    source_fd = os.open(source_path, flags)
    metadata = os.fstat(source_fd)
    trusted_owners = {0, os.geteuid()}
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in trusted_owners
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
    ):
        raise ValueError("untrusted uploader")

    private_directory = tempfile.mkdtemp(prefix="yinshi-uploader-", dir="/tmp")
    os.chmod(private_directory, 0o700)
    private_path = os.path.join(private_directory, "upload")
    target_fd = os.open(
        private_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o700,
    )
    with os.fdopen(source_fd, "rb") as source, os.fdopen(target_fd, "wb") as target:
        source_fd = -1
        shutil.copyfileobj(source, target)
    os.chmod(private_path, 0o700)
except (OSError, ValueError):
    if source_fd >= 0:
        os.close(source_fd)
    if private_directory:
        shutil.rmtree(private_directory, ignore_errors=True)
    print(
        "BACKUP_UPLOAD_COMMAND must be a trusted executable file",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(private_path)
PY
  )" || exit 1
  upload_private_dir="${upload_private_path%/*}"
  cleanup_upload() {
    if [[ -n "$upload_private_dir" ]]; then
      /bin/rm -rf -- "$upload_private_dir"
      upload_private_dir=""
    fi
  }
  trap cleanup_upload EXIT
  upload_status=0
  /usr/bin/env -i "PATH=/usr/bin:/bin" "$upload_private_path" "$archive_path" || upload_status=$?
  cleanup_upload
  if (( upload_status != 0 )); then
    exit "$upload_status"
  fi
elif [[ "${1:-}" == "--upload" ]]; then
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
elif [[ $# -ne 0 ]]; then
  printf 'Usage: %s [--upload]\n' "$0" >&2
  exit 1
fi

find "$backup_dir" -type f -name 'yinshi-*.tar.gz.enc' -mtime "+$retention_days" -delete
printf 'Encrypted backup complete: %s\n' "$archive_path"
