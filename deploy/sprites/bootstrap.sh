#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    printf 'Usage: %s <artifact-path> <sha256> <release-id>\n' "${0##*/}" >&2
}

if [[ $# -ne 3 ]]; then
    usage
    exit 64
fi

artifact=$1
expected_sha256=$2
release_id=$3
install_root=${YINSHI_INSTALL_ROOT:-/opt/yinshi}
state_root=${YINSHI_STATE_ROOT:-/var/lib/yinshi}
max_members=${YINSHI_MAX_ARCHIVE_MEMBERS:-10000}
max_bytes=${YINSHI_MAX_EXPANDED_BYTES:-536870912}

if [[ ! "$expected_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'SHA-256 must contain exactly 64 lowercase hexadecimal characters\n' >&2
    exit 64
fi
if [[ ! "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    printf 'Release ID contains invalid characters or length\n' >&2
    exit 64
fi
if [[ "$install_root" != /* || "$state_root" != /* ]]; then
    printf 'Install and state roots must be absolute paths\n' >&2
    exit 64
fi
if [[ ! "$max_members" =~ ^[0-9]+$ ]] || (( max_members < 1 || max_members > 10000 )); then
    printf 'Archive member limit is invalid\n' >&2
    exit 64
fi
if [[ ! "$max_bytes" =~ ^[0-9]+$ ]] || (( max_bytes < 1 || max_bytes > 536870912 )); then
    printf 'Expanded byte limit is invalid\n' >&2
    exit 64
fi
if [[ ! -f "$artifact" || -L "$artifact" ]]; then
    printf 'Artifact must be a local regular file\n' >&2
    exit 65
fi

storage_marker="$state_root/.yinshi-encrypted-storage"
if [[ ! -f "$storage_marker" || -L "$storage_marker" ]]; then
    printf 'Encrypted storage marker is required\n' >&2
    exit 65
fi
if [[ -L "$install_root" || -L "$state_root" ]]; then
    printf 'Install and state roots must not be symbolic links\n' >&2
    exit 65
fi

releases="$install_root/releases"
current="$install_root/current"
release="$releases/$release_id"
staging="$releases/.${release_id}.staging.$$"
temporary_link="$install_root/.current.$$"
private_artifact=""
candidate=""

cleanup() {
    status=$?
    trap - EXIT
    rm -rf -- "$staging"
    rm -f -- "$temporary_link"
    if [[ -n "$private_artifact" ]]; then
        rm -f -- "$private_artifact"
    fi
    if [[ -n "$candidate" ]]; then
        rm -rf -- "$candidate"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

private_artifact=$(mktemp "$state_root/.yinshi-artifact.XXXXXXXXXX")
chmod 0600 "$private_artifact"
python3 - "$artifact" "$private_artifact" <<'PY'
import os
import shutil
import stat
import sys

source_path, staged_path = sys.argv[1:]
source_flags = os.O_RDONLY
if hasattr(os, "O_CLOEXEC"):
    source_flags |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    source_flags |= os.O_NOFOLLOW
source_descriptor = os.open(source_path, source_flags)
source_info = os.fstat(source_descriptor)
if not stat.S_ISREG(source_info.st_mode):
    raise SystemExit("Artifact must remain a local regular file")

staged_flags = os.O_WRONLY | os.O_TRUNC
if hasattr(os, "O_CLOEXEC"):
    staged_flags |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    staged_flags |= os.O_NOFOLLOW
staged_descriptor = os.open(staged_path, staged_flags)
staged_info = os.fstat(staged_descriptor)
if not stat.S_ISREG(staged_info.st_mode) or staged_info.st_uid != os.geteuid():
    raise SystemExit("Private artifact staging file is invalid")

with os.fdopen(source_descriptor, "rb") as source_file:
    with os.fdopen(staged_descriptor, "wb") as staged_file:
        shutil.copyfileobj(source_file, staged_file, length=1024 * 1024)
        staged_file.flush()
        os.fsync(staged_file.fileno())
PY
chmod 0400 "$private_artifact"

if [[ -e "$release" || -L "$release" ]]; then
    printf 'Release already exists\n' >&2
    exit 65
fi
if [[ -e "$current" && ! -L "$current" ]]; then
    printf 'Current path must be a symbolic link\n' >&2
    exit 65
fi

python3 - "$private_artifact" "$expected_sha256" "$staging" "$max_members" "$max_bytes" <<'PY'
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile

(
    artifact_path,
    expected_sha256,
    staging_path,
    member_limit_text,
    byte_limit_text,
) = sys.argv[1:]
member_limit = int(member_limit_text)
byte_limit = int(byte_limit_text)
staging = Path(staging_path).resolve()
required_files = {
    "deploy",
    "deploy/sprites",
    "backend/pyproject.toml",
    "backend/requirements/base.txt",
    "backend/requirements/base.lock",
    "deploy/sprites/bootstrap.sh",
    "sidecar/package.json",
    "sidecar/package-lock.json",
}

artifact_flags = os.O_RDONLY
if hasattr(os, "O_CLOEXEC"):
    artifact_flags |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    artifact_flags |= os.O_NOFOLLOW
artifact_descriptor = os.open(artifact_path, artifact_flags)
artifact_info = os.fstat(artifact_descriptor)
if not stat.S_ISREG(artifact_info.st_mode) or artifact_info.st_uid != os.geteuid():
    raise SystemExit("Private artifact staging file is invalid")
os.unlink(artifact_path)
artifact_file = os.fdopen(artifact_descriptor, "rb")
value = hashlib.sha256()
for block in iter(lambda: artifact_file.read(1024 * 1024), b""):
    value.update(block)
if not hmac.compare_digest(value.hexdigest(), expected_sha256):
    raise SystemExit("Artifact checksum verification failed")
artifact_file.seek(0)

releases = staging.parent
install_root = releases.parent
install_root.mkdir(mode=0o700, parents=True, exist_ok=True)
releases.mkdir(mode=0o700, exist_ok=True)
os.chmod(install_root, 0o700)
os.chmod(releases, 0o700)
staging.mkdir(mode=0o700)

with tarfile.open(fileobj=artifact_file, mode="r:gz") as archive:
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    expanded_bytes = 0
    for member in archive:
        if len(members) >= member_limit:
            raise SystemExit("Archive contains too many members")
        raw_name = member.name
        name = raw_name.rstrip("/")
        if not name or "\\" in name or raw_name.startswith("/"):
            raise SystemExit("Archive member path is invalid")
        path = PurePosixPath(name)
        if any(part in {"", ".", ".."} for part in path.parts):
            raise SystemExit("Archive member path is invalid")
        is_runtime_member = path.parts[0] in {"backend", "sidecar"}
        is_bootstrap_member = name in {
            "deploy",
            "deploy/sprites",
            "deploy/sprites/bootstrap.sh",
        }
        if str(path) != name or not (is_runtime_member or is_bootstrap_member):
            raise SystemExit("Archive contains an unexpected root")
        if name in names:
            raise SystemExit("Archive contains duplicate members")
        if not (member.isdir() or member.isfile()):
            raise SystemExit("Archive links and special files are not allowed")
        if name in {"deploy", "deploy/sprites"} and not member.isdir():
            raise SystemExit("Archive bootstrap parent type is invalid")
        if name == "deploy/sprites/bootstrap.sh" and not member.isfile():
            raise SystemExit("Archive bootstrap file type is invalid")
        if member.size < 0:
            raise SystemExit("Archive member size is invalid")
        if member.isfile():
            expanded_bytes += member.size
            if expanded_bytes > byte_limit:
                raise SystemExit("Archive expands beyond the byte limit")
        names.add(name)
        members.append(member)

    if not required_files.issubset(names):
        raise SystemExit("Archive is missing required runtime metadata")

    for member in members:
        name = member.name.rstrip("/")
        destination = staging.joinpath(*PurePosixPath(name).parts)
        try:
            destination.relative_to(staging)
        except ValueError as error:
            raise SystemExit("Archive member escapes staging directory") from error
        if member.isdir():
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(destination, 0o700)
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in (destination.parent, *destination.parent.parents):
            if parent == staging.parent:
                break
            os.chmod(parent, 0o700)
            if parent == staging:
                break
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit("Archive regular file has no content")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                copied = shutil.copyfileobj(source, output, length=1024 * 1024)
            if destination.stat().st_size != member.size:
                raise SystemExit("Archive member size changed during extraction")
            os.chmod(destination, 0o700 if member.mode & 0o111 else 0o600)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
artifact_file.close()
PY

printf '%s\n' "$expected_sha256" > "$staging/.artifact-sha256"
chmod 0600 "$staging/.artifact-sha256"

python3 -m venv "$staging/venv"
"$staging/venv/bin/pip" install --require-hashes --requirement "$staging/backend/requirements/base.lock"
"$staging/venv/bin/pip" install --no-build-isolation --no-deps "$staging/backend"
(
    cd "$staging/sidecar"
    npm ci --omit=dev
)
chmod -R go-rwx "$staging"
mv -- "$staging" "$release"
candidate=$release
ln -s "releases/$release_id" "$temporary_link"
python3 - "$temporary_link" "$current" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
candidate=""
rm -f -- "$artifact"
trap - EXIT HUP INT TERM
printf 'Installed managed Sprite release %s\n' "$release_id"
