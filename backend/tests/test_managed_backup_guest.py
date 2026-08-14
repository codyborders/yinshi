"""Tests for encrypted managed guest archives."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_data_key(sqlite_root: Path, value: bytes = b"d" * 32) -> Path:
    key_path = sqlite_root / ".yinshi-data-protection-key"
    key_path.write_bytes(value)
    key_path.chmod(0o600)
    return key_path


def test_create_archive_encrypts_both_guest_data_roots(tmp_path: Path) -> None:
    """Backup creation should emit ciphertext for SQLite and shared files."""
    from yinshi.managed_backup_guest import (
        ManagedArchiveContext,
        create_managed_backup_archive,
        inspect_managed_backup_archive,
    )

    state_root = tmp_path / "state"
    sqlite_root = state_root / "sqlite"
    files_root = state_root / "files"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    (sqlite_root / "control.db").write_bytes(b"sqlite-control-secret")
    _write_data_key(sqlite_root)
    (files_root / "workspace.txt").write_text("workspace-secret", encoding="utf-8")
    archive_path = state_root / "maintenance" / "archive.enc"
    context = ManagedArchiveContext(
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        created_at="2026-08-12T12:00:00+00:00",
        owner_digest="a" * 64,
        runtime_generation=4,
    )

    record = create_managed_backup_archive(
        sqlite_root=sqlite_root,
        files_root=files_root,
        archive_path=archive_path,
        archive_key=b"k" * 32,
        context=context,
    )

    ciphertext = archive_path.read_bytes()
    assert b"sqlite-control-secret" not in ciphertext
    assert b"workspace-secret" not in ciphertext
    assert record.size_bytes == len(ciphertext)
    assert len(record.sha256) == 64
    assert inspect_managed_backup_archive(
        archive_path,
        archive_key=b"k" * 32,
        expected_context=context,
    ) == (
        "files/workspace.txt",
        "sqlite/.yinshi-data-protection-key",
        "sqlite/control.db",
    )


def test_create_archive_requires_portable_data_key(tmp_path: Path) -> None:
    """Backup creation should reject roots without portable key material."""
    from yinshi.managed_backup_guest import (
        ManagedArchiveContext,
        create_managed_backup_archive,
    )

    sqlite_root = tmp_path / "sqlite"
    files_root = tmp_path / "files"
    sqlite_root.mkdir()
    files_root.mkdir()

    with pytest.raises(ValueError, match="portable data-protection key"):
        create_managed_backup_archive(
            sqlite_root=sqlite_root,
            files_root=files_root,
            archive_path=tmp_path / "archive.enc",
            archive_key=b"k" * 32,
            context=ManagedArchiveContext(
                archive_id="archive",
                created_at="2026-08-13T00:00:00+00:00",
                owner_digest="a" * 64,
                runtime_generation=1,
            ),
        )


@pytest.mark.parametrize(
    ("key_value", "mode"),
    [(b"short", 0o600), (b"d" * 32, 0o644)],
)
def test_create_archive_rejects_invalid_portable_data_key(
    tmp_path: Path,
    key_value: bytes,
    mode: int,
) -> None:
    """Backup creation should reject malformed or permissive portable keys."""
    from yinshi.managed_backup_guest import (
        ManagedArchiveContext,
        create_managed_backup_archive,
    )

    sqlite_root = tmp_path / "sqlite"
    files_root = tmp_path / "files"
    sqlite_root.mkdir()
    files_root.mkdir()
    key_path = _write_data_key(sqlite_root, key_value)
    key_path.chmod(mode)

    with pytest.raises(ValueError, match="portable data-protection key"):
        create_managed_backup_archive(
            sqlite_root=sqlite_root,
            files_root=files_root,
            archive_path=tmp_path / "archive.enc",
            archive_key=b"k" * 32,
            context=ManagedArchiveContext(
                archive_id="archive",
                created_at="2026-08-13T00:00:00+00:00",
                owner_digest="a" * 64,
                runtime_generation=1,
            ),
        )


def test_create_archive_rejects_symlink_portable_data_key(tmp_path: Path) -> None:
    """Portable key source must not be a symlink."""
    from yinshi.managed_backup_guest import ManagedArchiveContext, create_managed_backup_archive

    sqlite_root = tmp_path / "sqlite"
    files_root = tmp_path / "files"
    sqlite_root.mkdir()
    files_root.mkdir()
    original = tmp_path / "key"
    original.write_bytes(b"s" * 32)
    original.chmod(0o600)
    (sqlite_root / ".yinshi-data-protection-key").symlink_to(original)

    with pytest.raises(ValueError):
        create_managed_backup_archive(
            sqlite_root=sqlite_root,
            files_root=files_root,
            archive_path=tmp_path / "archive.enc",
            archive_key=b"k" * 32,
            context=ManagedArchiveContext(
                archive_id="archive",
                created_at="2026-08-13T00:00:00+00:00",
                owner_digest="a" * 64,
                runtime_generation=1,
            ),
        )


def test_create_archive_rejects_hardlinked_portable_data_key(tmp_path: Path) -> None:
    """Portable key source must have exactly one filesystem link."""
    from yinshi.managed_backup_guest import ManagedArchiveContext, create_managed_backup_archive

    sqlite_root = tmp_path / "sqlite"
    files_root = tmp_path / "files"
    sqlite_root.mkdir()
    files_root.mkdir()
    original = tmp_path / "key"
    original.write_bytes(b"s" * 32)
    original.chmod(0o600)
    (sqlite_root / ".yinshi-data-protection-key").hardlink_to(original)

    with pytest.raises(ValueError):
        create_managed_backup_archive(
            sqlite_root=sqlite_root,
            files_root=files_root,
            archive_path=tmp_path / "archive.enc",
            archive_key=b"k" * 32,
            context=ManagedArchiveContext(
                archive_id="archive",
                created_at="2026-08-13T00:00:00+00:00",
                owner_digest="a" * 64,
                runtime_generation=1,
            ),
        )


@pytest.mark.parametrize("defect", ["missing", "size", "mode", "duplicate", "alias", "directory"])
def test_restore_rejects_invalid_portable_key_member_before_publication(
    tmp_path: Path,
    defect: str,
) -> None:
    """Authenticated malformed key members must not replace live roots."""
    import json
    import tarfile

    import yinshi.managed_backup_guest as guest

    context = guest.ManagedArchiveContext(
        archive_id="archive",
        created_at="2026-08-13T00:00:00+00:00",
        owner_digest="a" * 64,
        runtime_generation=1,
    )
    key_info = tarfile.TarInfo("sqlite/.yinshi-data-protection-key")
    key_info.mode = 0o600
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    if defect != "missing":
        members.append((key_info, b"s" * (31 if defect == "size" else 32)))
    if defect == "mode":
        key_info.mode = 0o644
    elif defect == "duplicate":
        duplicate = tarfile.TarInfo("sqlite/.yinshi-data-protection-key")
        duplicate.mode = 0o600
        members.append((duplicate, b"s" * 32))
    elif defect == "alias":
        key_info.name = "sqlite/./.yinshi-data-protection-key"
    elif defect == "directory":
        key_info.type = tarfile.DIRTYPE
    names = [member.name for member, _content in members]
    manifest = (
        json.dumps(
            {
                "context": guest.asdict(context),
                "format": "yinshi-managed-backup-v2",
                "members": sorted(names),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    tar_path = tmp_path / "crafted.tar"
    with tarfile.open(tar_path, "w") as archive:
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest)
        archive.addfile(manifest_info, guest.io.BytesIO(manifest))
        for member, content in members:
            member.size = len(content)
            archive.addfile(member, guest.io.BytesIO(content))
    archive_path = tmp_path / "crafted.enc"
    guest._encrypt_file(tar_path, archive_path, b"k" * 32)
    sqlite_root = tmp_path / "live" / "sqlite"
    files_root = tmp_path / "live" / "files"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    (sqlite_root / "sentinel").write_text("sqlite", encoding="utf-8")
    (files_root / "sentinel").write_text("files", encoding="utf-8")

    with pytest.raises(ValueError):
        guest.restore_managed_backup_archive(
            archive_path,
            archive_key=b"k" * 32,
            expected_context=context,
            sqlite_root=sqlite_root,
            files_root=files_root,
        )

    assert (sqlite_root / "sentinel").read_text(encoding="utf-8") == "sqlite"
    assert (files_root / "sentinel").read_text(encoding="utf-8") == "files"


def test_source_loss_inspection_rejects_legacy_v1_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source-loss restore should reject archives without portable-key format."""
    import json

    import yinshi.managed_backup_guest as guest

    sqlite_root = tmp_path / "sqlite"
    files_root = tmp_path / "files"
    sqlite_root.mkdir()
    files_root.mkdir()
    _write_data_key(sqlite_root)
    context = guest.ManagedArchiveContext(
        archive_id="archive",
        created_at="2026-08-13T00:00:00+00:00",
        owner_digest="a" * 64,
        runtime_generation=1,
    )

    def legacy_manifest(
        supplied_context: guest.ManagedArchiveContext,
        member_names: tuple[str, ...],
    ) -> bytes:
        return (
            json.dumps(
                {
                    "context": guest.asdict(supplied_context),
                    "format": "yinshi-managed-backup-v1",
                    "members": list(member_names),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()

    monkeypatch.setattr(guest, "_manifest", legacy_manifest)
    archive_path = tmp_path / "legacy.enc"
    guest.create_managed_backup_archive(
        sqlite_root=sqlite_root,
        files_root=files_root,
        archive_path=archive_path,
        archive_key=b"k" * 32,
        context=context,
    )

    with pytest.raises(ValueError, match="portable data-key support"):
        guest.inspect_managed_backup_archive(
            archive_path,
            archive_key=b"k" * 32,
            expected_context=context,
        )


def test_restore_archive_replaces_both_roots_and_preserves_runner_identity(
    tmp_path: Path,
) -> None:
    """Restore should replace guest data without replacing the runner identity."""
    from yinshi.managed_backup_guest import (
        ManagedArchiveContext,
        create_managed_backup_archive,
        restore_managed_backup_archive,
    )

    source = tmp_path / "source"
    source_sqlite = source / "sqlite"
    source_files = source / "files"
    source_sqlite.mkdir(parents=True)
    source_files.mkdir()
    (source_sqlite / "control.db").write_bytes(b"original-database")
    _write_data_key(source_sqlite, b"s" * 32)
    (source_files / "repo.txt").write_text("original-workspace", encoding="utf-8")
    context = ManagedArchiveContext(
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        created_at="2026-08-12T12:00:00+00:00",
        owner_digest="b" * 64,
        runtime_generation=5,
    )
    archive_path = tmp_path / "archive.enc"
    create_managed_backup_archive(
        sqlite_root=source_sqlite,
        files_root=source_files,
        archive_path=archive_path,
        archive_key=b"r" * 32,
        context=context,
    )

    state_root = tmp_path / "guest"
    sqlite_root = state_root / "sqlite"
    files_root = state_root / "files"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    (sqlite_root / "control.db").write_bytes(b"new-database")
    (files_root / "repo.txt").write_text("new-workspace", encoding="utf-8")
    identity_path = state_root / "runner-noise.key"
    identity_path.write_bytes(b"identity-must-survive")

    restore_managed_backup_archive(
        archive_path,
        archive_key=b"r" * 32,
        expected_context=context,
        sqlite_root=sqlite_root,
        files_root=files_root,
    )

    assert (sqlite_root / "control.db").read_bytes() == b"original-database"
    assert (files_root / "repo.txt").read_text(encoding="utf-8") == "original-workspace"
    assert identity_path.read_bytes() == b"identity-must-survive"
    assert not list(state_root.glob(".yinshi-restore-*"))


def test_restore_keeps_candidate_noise_and_reopens_source_worker_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restored storage uses source data key while transport identity stays fresh."""
    from yinshi.managed_backup_guest import (
        ManagedArchiveContext,
        create_managed_backup_archive,
        restore_managed_backup_archive,
    )
    from yinshi.runner_worker import RunnerWorkerManager

    source = tmp_path / "source"
    source_sqlite = source / "sqlite"
    source_files = source / "files"
    source_runtime = source / "runtime"
    source_sqlite.mkdir(parents=True, mode=0o700)
    source_files.mkdir(mode=0o700)
    source_key = b"s" * 32
    _write_data_key(source_sqlite, source_key)
    first = RunnerWorkerManager(
        data_directory=source_runtime,
        database_directory=source_sqlite,
        user_data_directory=source_files,
        data_protection_key=source_key,
        environment_setter=monkeypatch.setenv,
    ).dispatcher("account-1")
    source_bearer = first._principal.bearer_token
    context = ManagedArchiveContext(
        archive_id="archive",
        created_at="2026-08-13T00:00:00+00:00",
        owner_digest="a" * 64,
        runtime_generation=1,
    )
    archive_path = tmp_path / "archive.enc"
    create_managed_backup_archive(
        sqlite_root=source_sqlite,
        files_root=source_files,
        archive_path=archive_path,
        archive_key=b"k" * 32,
        context=context,
    )

    candidate = tmp_path / "candidate"
    candidate_sqlite = candidate / "sqlite"
    candidate_files = candidate / "files"
    candidate_runtime = candidate / "runtime"
    candidate_sqlite.mkdir(parents=True, mode=0o700)
    candidate_files.mkdir(mode=0o700)
    _write_data_key(candidate_sqlite, b"c" * 32)
    candidate_runtime.mkdir()
    candidate_runtime.chmod(0o700)
    noise_path = candidate_runtime / "runner-noise.key"
    noise_path.write_bytes(b"n" * 32)
    noise_path.chmod(0o600)

    restore_managed_backup_archive(
        archive_path,
        archive_key=b"k" * 32,
        expected_context=context,
        sqlite_root=candidate_sqlite,
        files_root=candidate_files,
    )

    assert noise_path.read_bytes() == b"n" * 32
    assert (candidate_sqlite / ".yinshi-data-protection-key").read_bytes() == source_key
    restored = RunnerWorkerManager(
        data_directory=candidate_runtime,
        database_directory=candidate_sqlite,
        user_data_directory=candidate_files,
        data_protection_key=source_key,
        environment_setter=monkeypatch.setenv,
    ).dispatcher("account-1")
    assert restored._principal.bearer_token == source_bearer
    with pytest.raises(Exception):
        RunnerWorkerManager(
            data_directory=tmp_path / "wrong-runtime",
            database_directory=candidate_sqlite,
            user_data_directory=candidate_files,
            data_protection_key=b"w" * 32,
            environment_setter=monkeypatch.setenv,
        )


def test_archive_with_empty_shared_root_remains_restorable(tmp_path: Path) -> None:
    """Empty durable roots should still be represented during restore."""
    from yinshi.managed_backup_guest import (
        ManagedArchiveContext,
        create_managed_backup_archive,
        restore_managed_backup_archive,
    )

    source = tmp_path / "source"
    source_sqlite = source / "sqlite"
    source_files = source / "files"
    source_sqlite.mkdir(parents=True)
    source_files.mkdir()
    (source_sqlite / "control.db").write_bytes(b"database")
    _write_data_key(source_sqlite)
    context = ManagedArchiveContext(
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        created_at="2026-08-12T12:00:00+00:00",
        owner_digest="e" * 64,
        runtime_generation=6,
    )
    archive_path = tmp_path / "archive.enc"
    create_managed_backup_archive(
        sqlite_root=source_sqlite,
        files_root=source_files,
        archive_path=archive_path,
        archive_key=b"q" * 32,
        context=context,
    )
    target = tmp_path / "target"
    target_sqlite = target / "sqlite"
    target_files = target / "files"
    target_sqlite.mkdir(parents=True)
    target_files.mkdir()
    (target_files / "stale.txt").write_text("remove me", encoding="utf-8")

    restore_managed_backup_archive(
        archive_path,
        archive_key=b"q" * 32,
        expected_context=context,
        sqlite_root=target_sqlite,
        files_root=target_files,
    )

    assert (target_sqlite / "control.db").read_bytes() == b"database"
    assert list(target_files.iterdir()) == []


def test_run_create_job_writes_fixed_private_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sealed create job should publish ciphertext and durable result metadata."""
    import base64
    import json
    import os
    import stat

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from yinshi.managed_backup_guest import run_managed_backup_job
    from yinshi.services.managed_backup_crypto import seal_managed_backup_job

    synchronized_directories = 0
    original_fsync = os.fsync

    def record_directory_sync(descriptor: int) -> None:
        nonlocal synchronized_directories
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synchronized_directories += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_directory_sync)
    state_root = tmp_path / "state"
    sqlite_root = state_root / "sqlite"
    files_root = state_root / "files"
    maintenance_root = state_root / "maintenance"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    (sqlite_root / "control.db").write_bytes(b"database")
    _write_data_key(sqlite_root)
    runner_key = X25519PrivateKey.generate()
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70"
    context = {
        "archive_id": "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        "created_at": "2026-08-12T12:00:00+00:00",
        "owner_digest": "a" * 64,
        "runtime_generation": 8,
    }
    envelope = seal_managed_backup_job(
        {
            "archive_context": context,
            "archive_key": base64.urlsafe_b64encode(b"j" * 32).rstrip(b"=").decode("ascii"),
            "job_id": job_id,
            "operation": "create",
            "version": 1,
        },
        runner_public_key=(
            base64.urlsafe_b64encode(runner_key.public_key().public_bytes_raw())
            .rstrip(b"=")
            .decode("ascii")
        ),
        job_id=job_id,
    )
    job_path = maintenance_root / f"{job_id}.job"
    result_path = maintenance_root / f"{job_id}.result"
    maintenance_root.mkdir(mode=0o700)
    job_path.write_bytes(envelope)
    job_path.chmod(0o600)

    run_managed_backup_job(
        job_path=job_path,
        result_path=result_path,
        runner_private_key=runner_key.private_bytes_raw(),
        sqlite_root=sqlite_root,
        files_root=files_root,
        maintenance_root=maintenance_root,
        expected_job_id=job_id,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert set(result) == {"job_id", "sha256", "size_bytes", "status"}
    assert result["job_id"] == job_id
    assert result["status"] == "ready"
    assert result["size_bytes"] > 0
    assert len(result["sha256"]) == 64
    assert (maintenance_root / f"{job_id}.archive.enc").is_file()
    assert result_path.stat().st_mode & 0o777 == 0o600
    assert synchronized_directories == 1


def test_run_create_job_reuses_valid_existing_result(tmp_path: Path) -> None:
    """A same-job retry should reuse completed encrypted output without overwriting it."""
    import base64
    import json

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from yinshi.managed_backup_guest import run_managed_backup_job
    from yinshi.services.managed_backup_crypto import seal_managed_backup_job

    state_root = tmp_path / "state"
    sqlite_root = state_root / "sqlite"
    files_root = state_root / "files"
    maintenance_root = state_root / "maintenance"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    maintenance_root.mkdir()
    (sqlite_root / "control.db").write_bytes(b"database")
    _write_data_key(sqlite_root)
    runner_key = X25519PrivateKey.generate()
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e80"
    envelope = seal_managed_backup_job(
        {
            "archive_context": {
                "archive_id": "archive-1",
                "created_at": "2026-08-12T12:00:00+00:00",
                "owner_digest": "a" * 64,
                "runtime_generation": 8,
            },
            "archive_key": base64.urlsafe_b64encode(b"j" * 32).rstrip(b"=").decode("ascii"),
            "job_id": job_id,
            "operation": "create",
            "version": 1,
        },
        runner_public_key=base64.urlsafe_b64encode(runner_key.public_key().public_bytes_raw())
        .rstrip(b"=")
        .decode("ascii"),
        job_id=job_id,
    )
    job_path = maintenance_root / f"{job_id}.job"
    result_path = maintenance_root / f"{job_id}.result"
    archive_path = maintenance_root / f"{job_id}.archive.enc"
    job_path.write_bytes(envelope)
    job_path.chmod(0o600)

    run_managed_backup_job(
        job_path=job_path,
        result_path=result_path,
        runner_private_key=runner_key.private_bytes_raw(),
        sqlite_root=sqlite_root,
        files_root=files_root,
        maintenance_root=maintenance_root,
        expected_job_id=job_id,
    )
    first_archive = archive_path.read_bytes()
    first_result = json.loads(result_path.read_text(encoding="utf-8"))

    run_managed_backup_job(
        job_path=job_path,
        result_path=result_path,
        runner_private_key=runner_key.private_bytes_raw(),
        sqlite_root=sqlite_root,
        files_root=files_root,
        maintenance_root=maintenance_root,
        expected_job_id=job_id,
    )

    assert archive_path.read_bytes() == first_archive
    assert json.loads(result_path.read_text(encoding="utf-8")) == first_result


def test_run_restore_job_replaces_data_and_writes_private_result(tmp_path: Path) -> None:
    """A sealed restore job should replace both roots and report exact completion."""
    import base64
    import json

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from yinshi.managed_backup_guest import (
        ManagedArchiveContext,
        create_managed_backup_archive,
        run_managed_backup_job,
    )
    from yinshi.services.managed_backup_crypto import seal_managed_backup_job

    source = tmp_path / "source"
    source_sqlite = source / "sqlite"
    source_files = source / "files"
    source_sqlite.mkdir(parents=True)
    source_files.mkdir()
    (source_sqlite / "control.db").write_bytes(b"restored-database")
    _write_data_key(source_sqlite)
    (source_files / "workspace.txt").write_bytes(b"restored-files")
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e79"
    context = ManagedArchiveContext(
        archive_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e7a",
        created_at="2026-08-12T12:00:00+00:00",
        owner_digest="a" * 64,
        runtime_generation=8,
    )
    archive_key = b"j" * 32
    maintenance_root = tmp_path / "state" / "maintenance"
    maintenance_root.mkdir(parents=True)
    archive_path = maintenance_root / f"{job_id}.archive.enc"
    create_managed_backup_archive(
        sqlite_root=source_sqlite,
        files_root=source_files,
        archive_path=archive_path,
        archive_key=archive_key,
        context=context,
    )
    sqlite_root = tmp_path / "state" / "sqlite"
    files_root = tmp_path / "state" / "files"
    sqlite_root.mkdir()
    files_root.mkdir()
    (sqlite_root / "control.db").write_bytes(b"stale")
    (files_root / "workspace.txt").write_bytes(b"stale")
    runner_key = X25519PrivateKey.generate()
    envelope = seal_managed_backup_job(
        {
            "archive_context": {
                "archive_id": context.archive_id,
                "created_at": context.created_at,
                "owner_digest": context.owner_digest,
                "runtime_generation": context.runtime_generation,
            },
            "archive_key": base64.urlsafe_b64encode(archive_key).rstrip(b"=").decode("ascii"),
            "job_id": job_id,
            "operation": "restore",
            "version": 1,
        },
        runner_public_key=(
            base64.urlsafe_b64encode(runner_key.public_key().public_bytes_raw())
            .rstrip(b"=")
            .decode("ascii")
        ),
        job_id=job_id,
    )
    job_path = maintenance_root / f"{job_id}.job"
    result_path = maintenance_root / f"{job_id}.result"
    job_path.write_bytes(envelope)
    job_path.chmod(0o600)

    run_managed_backup_job(
        job_path=job_path,
        result_path=result_path,
        runner_private_key=runner_key.private_bytes_raw(),
        sqlite_root=sqlite_root,
        files_root=files_root,
        maintenance_root=maintenance_root,
        expected_job_id=job_id,
    )

    assert (sqlite_root / "control.db").read_bytes() == b"restored-database"
    assert (files_root / "workspace.txt").read_bytes() == b"restored-files"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result == {
        "cleanup_pending": False,
        "job_id": job_id,
        "status": "restored",
    }
    assert result_path.stat().st_mode & 0o777 == 0o600


def test_guest_command_uses_fixed_managed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guest command should derive every path from its fixed state root."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    import yinshi.managed_backup_guest as guest
    from yinshi.services.managed_backup_crypto import seal_managed_backup_job

    state_root = tmp_path / "state"
    sqlite_root = state_root / "sqlite"
    files_root = state_root / "files"
    maintenance_root = state_root / "maintenance"
    sqlite_root.mkdir(parents=True)
    files_root.mkdir()
    maintenance_root.mkdir(mode=0o700)
    (sqlite_root / "control.db").write_bytes(b"database")
    _write_data_key(sqlite_root)
    runner_key = X25519PrivateKey.generate()
    runner_key_path = state_root / "runner-noise.key"
    runner_key_path.write_bytes(runner_key.private_bytes_raw())
    runner_key_path.chmod(0o600)
    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e71"
    archive_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e72"
    archive_key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii")
    envelope = seal_managed_backup_job(
        {
            "archive_context": {
                "archive_id": archive_id,
                "created_at": "2026-08-12T12:00:00+00:00",
                "owner_digest": "a" * 64,
                "runtime_generation": 8,
            },
            "archive_key": archive_key,
            "job_id": job_id,
            "operation": "create",
            "version": 1,
        },
        runner_public_key=(
            base64.urlsafe_b64encode(runner_key.public_key().public_bytes_raw())
            .rstrip(b"=")
            .decode("ascii")
        ),
        job_id=job_id,
    )
    (maintenance_root / f"{job_id}.job").write_bytes(envelope)
    monkeypatch.setattr(guest, "_STATE_ROOT", state_root)

    assert guest.main(["create", "--job-id", job_id]) == 0
    assert (maintenance_root / f"{job_id}.archive.enc").is_file()
    assert (maintenance_root / f"{job_id}.result").is_file()


@pytest.mark.asyncio
async def test_held_guest_job_keeps_task_until_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held jobs should retain a task until control writes the exact release marker."""
    import yinshi.managed_backup_guest as guest

    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e74"
    maintenance_root = tmp_path / "maintenance"
    maintenance_root.mkdir()
    paths = [
        maintenance_root / f"{job_id}.job",
        maintenance_root / f"{job_id}.result",
        maintenance_root / f"{job_id}.archive.enc",
    ]
    for path in paths:
        path.write_bytes(b"value")
    operations: list[str] = []

    class Lease:
        async def acquire(self) -> None:
            operations.append("acquire")

        async def aclose(self) -> None:
            operations.append("close")

    async def release_after_result(_delay: float) -> None:
        operations.append("wait")
        (maintenance_root / f"{job_id}.release").write_bytes(b"release\n")

    monkeypatch.setattr(guest, "SpriteTaskLease", Lease)
    monkeypatch.setattr(guest.asyncio, "sleep", release_after_result)

    await guest.hold_managed_backup_job(
        job_id=job_id,
        maintenance_root=maintenance_root,
        run_job=lambda: operations.append("run"),
        timeout_seconds=5,
    )

    assert operations == ["acquire", "run", "wait", "close"]
    assert not any(path.exists() for path in paths)
    assert not (maintenance_root / f"{job_id}.release").exists()


@pytest.mark.asyncio
async def test_held_guest_job_removes_incomplete_output_after_job_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed guest job must remove partial output that cannot be reconciled."""
    import yinshi.managed_backup_guest as guest

    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e76"
    maintenance_root = tmp_path / "maintenance"
    maintenance_root.mkdir()
    paths = [
        maintenance_root / f"{job_id}.job",
        maintenance_root / f"{job_id}.result",
        maintenance_root / f"{job_id}.archive.enc",
    ]
    paths[0].write_bytes(b"job")

    class Lease:
        async def acquire(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

    def fail_job() -> None:
        paths[2].write_bytes(b"partial")
        raise RuntimeError("guest archive failed")

    monkeypatch.setattr(guest, "SpriteTaskLease", Lease)

    with pytest.raises(RuntimeError, match="guest archive failed"):
        await guest.hold_managed_backup_job(
            job_id=job_id,
            maintenance_root=maintenance_root,
            run_job=fail_job,
            timeout_seconds=5,
        )

    assert not any(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_held_guest_job_preserves_completed_output_without_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost controller must not erase completed output before reconciliation."""
    import yinshi.managed_backup_guest as guest

    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e75"
    maintenance_root = tmp_path / "maintenance"
    maintenance_root.mkdir()
    paths = [
        maintenance_root / f"{job_id}.job",
        maintenance_root / f"{job_id}.result",
        maintenance_root / f"{job_id}.archive.enc",
    ]
    for path in paths:
        path.write_bytes(b"value")

    class Lease:
        async def acquire(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

    async def lose_controller(_delay: float) -> None:
        raise TimeoutError("controller unavailable")

    monkeypatch.setattr(guest, "SpriteTaskLease", Lease)
    monkeypatch.setattr(guest.asyncio, "sleep", lose_controller)

    with pytest.raises(TimeoutError, match="controller unavailable"):
        await guest.hold_managed_backup_job(
            job_id=job_id,
            maintenance_root=maintenance_root,
            run_job=lambda: None,
            timeout_seconds=5,
        )

    assert all(path.is_file() for path in paths)


def test_guest_cli_holds_create_job_until_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixed guest CLI should retain a Sprite task during archive transfer."""
    import yinshi.managed_backup_guest as guest

    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e74"
    maintenance_root = tmp_path / "maintenance"
    maintenance_root.mkdir()
    calls: list[tuple[str, int]] = []

    async def hold(**kwargs) -> None:
        calls.append((kwargs["job_id"], kwargs["timeout_seconds"]))

    monkeypatch.setattr(guest, "_STATE_ROOT", tmp_path)
    monkeypatch.setattr(guest, "hold_managed_backup_job", hold)

    assert guest.main(["create", "--job-id", job_id, "--hold-seconds", "60"]) == 0
    assert calls == [(job_id, 60)]


def test_guest_cli_accepts_fixed_restore_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixed guest CLI should dispatch restore through the same bounded holder."""
    import yinshi.managed_backup_guest as guest

    job_id = "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e7b"
    maintenance_root = tmp_path / "maintenance"
    maintenance_root.mkdir()
    calls: list[str] = []

    async def hold(**kwargs) -> None:
        calls.append(kwargs["job_id"])

    monkeypatch.setattr(guest, "_STATE_ROOT", tmp_path)
    monkeypatch.setattr(guest, "hold_managed_backup_job", hold)

    assert guest.main(["restore", "--job-id", job_id, "--hold-seconds", "60"]) == 0
    assert calls == [job_id]
