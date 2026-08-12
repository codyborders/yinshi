#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 2 ]]; then
    printf 'Usage: %s <git-commit> <output-path>\n' "${0##*/}" >&2
    exit 64
fi

selected_paths=(
    backend/pyproject.toml
    backend/requirements/base.txt
    backend/requirements/base.lock
    backend/src
    sidecar/package.json
    sidecar/package-lock.json
    sidecar/src
)
if [[ -n "$(git status --porcelain=v1 --untracked-files=all -- "${selected_paths[@]}")" ]]; then
    printf 'Selected deployment paths are dirty\n' >&2
    exit 66
fi

git archive --format=tar "$1" "${selected_paths[@]}" | gzip -n > "$2"
chmod 0600 "$2"
python3 - "$2" <<'PY'
import hashlib
import pathlib
import sys

artifact = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
checksum = pathlib.Path(f"{artifact}.sha256")
checksum.write_text(f"{digest}\n", encoding="ascii")
checksum.chmod(0o600)
PY
