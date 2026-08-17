"""Managed Sprite deployment artifact and bootstrap behavior tests."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import textwrap
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "deploy" / "sprites" / "build-artifact.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "deploy" / "sprites" / "bootstrap.sh"
BASE_REQUIREMENTS = REPO_ROOT / "backend" / "requirements" / "base.txt"
LOCKED_REQUIREMENTS = REPO_ROOT / "backend" / "requirements" / "base.lock"
PYPROJECT = REPO_ROOT / "backend" / "pyproject.toml"
SIDECAR_LOCK = REPO_ROOT / "sidecar" / "package-lock.json"


def test_deployment_scripts_exist() -> None:
    """Deployment entry points must exist at stable paths."""
    assert BUILD_SCRIPT.is_file()
    assert BOOTSTRAP_SCRIPT.is_file()


def test_deployment_scripts_are_executable() -> None:
    """Deployment entry points must be directly executable."""
    assert os.access(BUILD_SCRIPT, os.X_OK)
    assert os.access(BOOTSTRAP_SCRIPT, os.X_OK)


def test_managed_python_lock_hashes_exact_build_tools() -> None:
    """Managed lock hashes the exact build tools declared by the project."""
    source = BASE_REQUIREMENTS.read_text(encoding="utf-8")
    lock = LOCKED_REQUIREMENTS.read_text(encoding="utf-8")
    build_requires = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["build-system"][
        "requires"
    ]

    assert "--hash=" not in source
    assert "--hash=sha256:" in lock
    for requirement in build_requires:
        assert "==" in requirement
        assert not any(operator in requirement for operator in (">", "<", "~", "!"))
        assert requirement in source
        assert f"{requirement.lower()} \\" in lock.lower()


def test_sprite_example_allows_required_package_and_codex_hosts() -> None:
    """Managed installation and Codex authorization can reach required hosts."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert (
        "SPRITES_ALLOWED_DOMAINS="
        "registry.npmjs.org,nodejs.org,pypi.org,files.pythonhosted.org,"
        "auth.openai.com,chatgpt.com,api.openai.com,control.example.com\n" in example
    )


def test_sidecar_docker_disables_other_install_scripts() -> None:
    """Container installation should rebuild only the approved native module."""
    dockerfile = (REPO_ROOT / "sidecar" / "Dockerfile").read_text(encoding="utf-8")

    assert "npm ci --omit=dev --ignore-scripts" in dockerfile
    assert "npm rebuild node-pty --foreground-scripts" in dockerfile


def test_sidecar_allows_only_pinned_terminal_native_install_script() -> None:
    """Managed installation should build only the audited node-pty native module."""
    package = json.loads((REPO_ROOT / "sidecar" / "package.json").read_text(encoding="utf-8"))

    assert package["allowScripts"] == {"node-pty@1.1.0": True}


def test_sidecar_lock_has_integrity_for_downloaded_runtime_packages() -> None:
    """Downloaded runtime packages must carry registry integrity metadata."""
    packages = json.loads(SIDECAR_LOCK.read_text(encoding="utf-8"))["packages"]

    missing = sorted(
        path
        for path, metadata in packages.items()
        if path and not metadata.get("link", False) and not metadata.get("integrity")
    )

    assert missing == []


def _run(
    command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=False, capture_output=True, text=True)


def _git(command: list[str], cwd: Path) -> str:
    result = _run(["git", *command], cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _build_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    files = {
        "backend/pyproject.toml": "[project]\nname='yinshi'\n",
        "backend/requirements/base.txt": "fastapi==1.0\n",
        "backend/requirements/base.lock": "fastapi==1.0 --hash=sha256:abc\n",
        "backend/src/yinshi/__init__.py": "VERSION = 'tracked'\n",
        "backend/src/yinshi/.env": "SECRET=excluded\n",
        "backend/src/yinshi/__pycache__/cached.pyc": "excluded\n",
        "backend/src/tests/test_hidden.py": "excluded\n",
        "sidecar/package.json": '{"name":"sidecar"}\n',
        "sidecar/package-lock.json": '{"lockfileVersion":3}\n',
        "sidecar/src/index.js": "export const tracked = true;\n",
        "deploy/sprites/bootstrap.sh": "#!/usr/bin/env bash\n",
        "sidecar/src/.env.production": "SECRET=excluded\n",
        "sidecar/src/node_modules/pkg.js": "excluded\n",
        "outside.txt": "excluded\n",
    }
    for relative_path, content in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(["init", "-q"], repository)
    _git(["config", "user.email", "test@example.com"], repository)
    _git(["config", "user.name", "Test"], repository)
    _git(["add", "."], repository)
    _git(["commit", "-qm", "fixture"], repository)
    return repository, _git(["rev-parse", "HEAD"], repository)


def test_build_artifact_writes_lowercase_sha256(tmp_path: Path) -> None:
    """Build writes the exact lowercase artifact digest beside the output."""
    repository, commit = _build_repository(tmp_path)
    output = tmp_path / "release.tar.gz"

    result = _run([str(BUILD_SCRIPT), commit, str(output)], repository)

    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = Path(f"{output}.sha256")
    assert checksum.read_text(encoding="ascii") == f"{digest}\n"
    assert output.stat().st_mode & 0o777 == 0o600
    assert checksum.stat().st_mode & 0o777 == 0o600


def test_build_artifact_includes_managed_python_lock(tmp_path: Path) -> None:
    """Build includes the managed Python lock in the tracked artifact."""
    repository, commit = _build_repository(tmp_path)
    output = tmp_path / "release.tar.gz"

    result = _run([str(BUILD_SCRIPT), commit, str(output)], repository)

    assert result.returncode == 0, result.stderr
    with tarfile.open(output, mode="r:gz") as archive:
        assert "backend/requirements/base.lock" in archive.getnames()


def test_build_artifact_includes_managed_bootstrap_script(tmp_path: Path) -> None:
    """Build includes the audited bootstrap used to install the artifact."""
    repository, commit = _build_repository(tmp_path)
    output = tmp_path / "release.tar.gz"

    result = _run([str(BUILD_SCRIPT), commit, str(output)], repository)

    assert result.returncode == 0, result.stderr
    with tarfile.open(output, mode="r:gz") as archive:
        assert "deploy/sprites/bootstrap.sh" in archive.getnames()


def test_build_artifact_rejects_dirty_selected_paths(tmp_path: Path) -> None:
    """Build stops when selected deployment paths contain local changes."""
    repository, commit = _build_repository(tmp_path)
    (repository / "backend" / "src" / "yinshi" / "dirty.py").write_text(
        "dirty = True\n", encoding="utf-8"
    )
    output = tmp_path / "release.tar.gz"

    result = _run([str(BUILD_SCRIPT), commit, str(output)], repository)

    assert result.returncode != 0
    assert not output.exists()


def _write_artifact(path: Path, members: dict[str, bytes]) -> str:
    with tarfile.open(path, mode="w:gz") as archive:
        for directory_name in ("deploy", "deploy/sprites"):
            directory = tarfile.TarInfo(directory_name)
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
        for name, content in members.items():
            member = tarfile.TarInfo(name)
            member.mode = 0o644
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bootstrap_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    install_root = tmp_path / "install"
    state_root = tmp_path / "state"
    tools = tmp_path / "tools"
    log = tmp_path / "packages.log"
    state_root.mkdir()
    (state_root / ".yinshi-encrypted-storage").touch(mode=0o600)
    tools.mkdir()
    python_wrapper = tools / "python3"
    python_wrapper.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1-}" == "-m" && "${2-}" == "venv" ]]; then
                mkdir -p "$3/bin"
                cat > "$3/bin/pip" <<'PIP'
            #!/usr/bin/env bash
            printf 'pip:%s\\n' "$*" >> "$PACKAGE_LOG"
            exit "${FAKE_PIP_EXIT:-0}"
            PIP
                chmod 0700 "$3/bin/pip"
                exit 0
            fi
            if [[ -n "${ARTIFACT_REPLACEMENT_AFTER_PYTHON-}" && "${2-}" == "${ARTIFACT_PATH-}" ]]; then
                "$REAL_PYTHON" "$@"
                status=$?
                if [[ "$status" -eq 0 ]]; then
                    cp -- "$ARTIFACT_REPLACEMENT_AFTER_PYTHON" "$ARTIFACT_PATH"
                fi
                exit "$status"
            fi
            exec "$REAL_PYTHON" "$@"
            """),
        encoding="utf-8",
    )
    python_wrapper.chmod(0o700)
    npm = tools / "npm"
    npm.write_text(
        '#!/usr/bin/env bash\nprintf \'npm:%s\\n\' "$*" >> "$PACKAGE_LOG"\n'
        'exit "${FAKE_NPM_EXIT:-0}"\n',
        encoding="utf-8",
    )
    npm.chmod(0o700)
    sprite_env = tools / "sprite-env"
    sprite_env.write_text(
        '#!/usr/bin/env bash\nprintf \'sprite-env:%s\\n\' "$*" >> "$PACKAGE_LOG"\n'
        'if [[ "${FAKE_SPRITE_STOP_SIGNAL:-0}" == "1" '
        '&& "$*" == "services stop yinshi-bootstrap" ]]; then\n'
        '    kill -TERM "$PPID"\n'
        "fi\n",
        encoding="utf-8",
    )
    sprite_env.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tools}:{env['PATH']}",
            "REAL_PYTHON": os.environ.get("PYTHON", os.sys.executable),
            "PACKAGE_LOG": str(log),
            "YINSHI_INSTALL_ROOT": str(install_root),
            "YINSHI_STATE_ROOT": str(state_root),
        }
    )
    return env, install_root, log


def _valid_members() -> dict[str, bytes]:
    return {
        "backend/pyproject.toml": b"[project]\nname='yinshi'\n",
        "backend/requirements/base.txt": b"fastapi==1.0\n",
        "backend/requirements/base.lock": b"fastapi==1.0 --hash=sha256:abc\n",
        "backend/src/yinshi/__init__.py": b"",
        "deploy/sprites/bootstrap.sh": b"#!/usr/bin/env bash\n",
        "sidecar/package.json": b'{"name":"sidecar"}\n',
        "sidecar/package-lock.json": b'{"lockfileVersion":3}\n',
        "sidecar/src/index.js": b"",
    }


def _valid_artifact(path: Path) -> str:
    return _write_artifact(path, _valid_members())


def test_bootstrap_requires_existing_encryption_marker(tmp_path: Path) -> None:
    """Bootstrap never creates the required encrypted storage marker."""
    env, _, log = _bootstrap_environment(tmp_path)
    marker = Path(env["YINSHI_STATE_ROOT"]) / ".yinshi-encrypted-storage"
    marker.unlink()
    artifact = tmp_path / "release.tar.gz"
    digest = _valid_artifact(artifact)

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode != 0
    assert not marker.exists()
    assert artifact.exists()
    assert not log.exists()


def test_bootstrap_rejects_bootstrap_path_as_directory(tmp_path: Path) -> None:
    """Bootstrap path must contain the audited regular file, not a directory."""
    env, install_root, log = _bootstrap_environment(tmp_path)
    artifact = tmp_path / "release.tar.gz"
    members = _valid_members()
    members.pop("deploy/sprites/bootstrap.sh")
    with tarfile.open(artifact, mode="w:gz") as archive:
        for name, content in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        bootstrap = tarfile.TarInfo("deploy/sprites/bootstrap.sh")
        bootstrap.type = tarfile.DIRTYPE
        archive.addfile(bootstrap)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode != 0
    assert not (install_root / "current").exists()
    assert not log.exists()


@pytest.mark.parametrize("missing_parent", ["deploy", "deploy/sprites"])
def test_bootstrap_rejects_missing_bootstrap_parent_directory(
    tmp_path: Path, missing_parent: str
) -> None:
    """Artifact must declare both bootstrap parent directories explicitly."""
    env, install_root, log = _bootstrap_environment(tmp_path)
    artifact = tmp_path / "release.tar.gz"
    with tarfile.open(artifact, mode="w:gz") as archive:
        for name, content in _valid_members().items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        for directory_name in ("deploy", "deploy/sprites"):
            if directory_name == missing_parent:
                continue
            directory = tarfile.TarInfo(directory_name)
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode != 0
    assert not (install_root / "current").exists()
    assert not log.exists()


def test_bootstrap_rejects_checksum_mismatch_before_install(tmp_path: Path) -> None:
    """Bootstrap verifies the exact digest before package tools or extraction."""
    env, install_root, log = _bootstrap_environment(tmp_path)
    artifact = tmp_path / "release.tar.gz"
    _valid_artifact(artifact)

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), "0" * 64, "release-123"], tmp_path, env)

    assert result.returncode != 0
    assert artifact.exists()
    assert not (install_root / "releases").exists()
    assert not log.exists()


@pytest.mark.parametrize(
    ("bad_name", "bad_type", "link_name"),
    [
        ("../escape", tarfile.REGTYPE, ""),
        ("backend/src/link", tarfile.SYMTYPE, "/etc/passwd"),
        ("backend/src/device", tarfile.CHRTYPE, ""),
        ("unexpected/file", tarfile.REGTYPE, ""),
        ("deploy/hooks/payload", tarfile.REGTYPE, ""),
    ],
)
def test_bootstrap_rejects_unsafe_archive_members(
    tmp_path: Path, bad_name: str, bad_type: bytes, link_name: str
) -> None:
    """Bootstrap rejects traversal, links, devices, and unexpected roots."""
    env, install_root, log = _bootstrap_environment(tmp_path)
    artifact = tmp_path / "release.tar.gz"
    with tarfile.open(artifact, mode="w:gz") as archive:
        for directory_name in ("deploy", "deploy/sprites"):
            directory = tarfile.TarInfo(directory_name)
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
        for name, content in _valid_members().items():
            safe_member = tarfile.TarInfo(name)
            safe_member.size = len(content)
            archive.addfile(safe_member, io.BytesIO(content))
        member = tarfile.TarInfo(bad_name)
        member.type = bad_type
        member.linkname = link_name
        if bad_type == tarfile.REGTYPE:
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        else:
            archive.addfile(member)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode != 0
    assert artifact.exists()
    assert not (install_root / "releases" / "release-123").exists()
    assert not log.exists()


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [("YINSHI_MAX_ARCHIVE_MEMBERS", "5"), ("YINSHI_MAX_EXPANDED_BYTES", "10")],
)
def test_bootstrap_enforces_archive_limits(
    tmp_path: Path, limit_name: str, limit_value: str
) -> None:
    """Bootstrap caps archive member count and expanded regular-file bytes."""
    env, install_root, _ = _bootstrap_environment(tmp_path)
    env[limit_name] = limit_value
    artifact = tmp_path / "release.tar.gz"
    digest = _valid_artifact(artifact)

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode != 0
    assert artifact.exists()
    assert not (install_root / "releases" / "release-123").exists()


def test_bootstrap_extracts_the_bytes_that_passed_digest_validation(tmp_path: Path) -> None:
    """Changing the upload path after validation cannot change installed bytes."""
    env, install_root, _ = _bootstrap_environment(tmp_path)
    artifact = tmp_path / "release.tar.gz"
    trusted_members = _valid_members()
    trusted_members["backend/src/yinshi/__init__.py"] = b"VERSION = 'trusted'\n"
    digest = _write_artifact(artifact, trusted_members)
    replacement = tmp_path / "replacement.tar.gz"
    changed_members = _valid_members()
    changed_members["backend/src/yinshi/__init__.py"] = b"VERSION = 'changed'\n"
    _write_artifact(replacement, changed_members)
    env["ARTIFACT_PATH"] = str(artifact)
    env["ARTIFACT_REPLACEMENT_AFTER_PYTHON"] = str(replacement)

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode == 0, result.stderr
    installed = install_root / "releases" / "release-123" / "backend/src/yinshi/__init__.py"
    assert installed.read_bytes() == trusted_members["backend/src/yinshi/__init__.py"]
    assert not list(Path(env["YINSHI_STATE_ROOT"]).glob(".yinshi-artifact.*"))


def test_bootstrap_preserves_current_and_cleans_staging_on_install_failure(
    tmp_path: Path,
) -> None:
    """Package failure leaves current unchanged and removes private staging."""
    env, install_root, log = _bootstrap_environment(tmp_path)
    env["FAKE_NPM_EXIT"] = "9"
    old_release = install_root / "releases" / "old-release"
    old_release.mkdir(parents=True)
    current = install_root / "current"
    current.symlink_to("releases/old-release")
    artifact = tmp_path / "release.tar.gz"
    digest = _valid_artifact(artifact)

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode != 0
    assert current.resolve() == old_release.resolve()
    assert artifact.exists()
    assert not (install_root / "releases" / "release-123").exists()
    assert not list((install_root / "releases").glob(".*.staging.*"))
    assert not list(Path(env["YINSHI_STATE_ROOT"]).glob(".yinshi-artifact.*"))
    calls = log.read_text(encoding="utf-8")
    assert "npm:ci --omit=dev --ignore-scripts" in calls
    assert "npm:rebuild node-pty --foreground-scripts" not in calls
    assert "sprite-env:" not in calls


def test_bootstrap_installs_release_and_switches_current(tmp_path: Path) -> None:
    """Bootstrap validates, installs, switches, removes upload, and exits cleanly."""
    env, install_root, log = _bootstrap_environment(tmp_path)
    env["FAKE_SPRITE_STOP_SIGNAL"] = "1"
    artifact = tmp_path / "release.tar.gz"
    digest = _valid_artifact(artifact)

    result = _run([str(BOOTSTRAP_SCRIPT), str(artifact), digest, "release-123"], tmp_path, env)

    assert result.returncode == 0, result.stderr
    release = install_root / "releases" / "release-123"
    assert (release / "backend" / "src" / "yinshi" / "__init__.py").is_file()
    assert (release / "deploy" / "sprites" / "bootstrap.sh").is_file()
    attestation = release / ".artifact-sha256"
    assert attestation.is_file()
    assert not attestation.is_symlink()
    assert attestation.read_text(encoding="ascii") == f"{digest}\n"
    assert attestation.stat().st_mode & 0o777 == 0o600
    assert (install_root / "current").resolve() == release.resolve()
    assert not artifact.exists()
    calls = log.read_text(encoding="utf-8")
    package_calls = calls.splitlines()
    assert package_calls[0].startswith("pip:install --require-hashes --requirement ")
    assert package_calls[0].endswith("/backend/requirements/base.lock")
    assert package_calls[1].startswith("pip:install --no-build-isolation --no-deps ")
    assert package_calls[1].endswith("/backend")
    assert "base.txt" not in calls
    assert "npm:ci --omit=dev --ignore-scripts" in calls
    assert "npm:rebuild node-pty --foreground-scripts" in calls
    assert "sprite-env:services stop yinshi-bootstrap" not in calls
