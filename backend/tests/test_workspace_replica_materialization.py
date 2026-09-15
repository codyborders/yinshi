"""Materialize authoritative replica artifacts into launcher-compatible trees."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from yinshi.exceptions import GitError
from yinshi.root_launcher import LaunchLayout, ReplicaLifecycleValidator
from yinshi.services.workspace_replica_artifact import (
    ManifestEntry,
    WorktreeArtifactInput,
    WorktreeEntryKind,
    decode_worktree_artifact,
    encode_worktree_artifact,
)
from yinshi.services.workspace_replica_bundle import create_committed_bundle
from yinshi.services.workspace_replica_materialization import (
    ReplicaMaterializationCollisionError,
    ReplicaMaterializationRejectedError,
    ReplicaMaterializationRequest,
    ReplicaMaterializationStore,
    ReplicaMaterializationUnresolvedError,
)
from yinshi.services.workspace_replica_object_pack import create_index_object_pack
from yinshi.services.workspace_replica_publication import (
    ReplicaArtifactBinding,
    ReplicaArtifactSetDeclaration,
    ReplicaIdentity,
    ReplicaStoreLimits,
    WorkspaceReplicaPublicationStore,
)


def git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout.strip()


def private_root(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


@pytest.fixture(autouse=True)
def modeled_mount_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from yinshi.services import workspace_replica_materialization as module

    monkeypatch.setattr(
        module,
        "_mount_id",
        lambda descriptor: os.fstat(descriptor).st_dev,
        raising=False,
    )


def test_isolated_git_config_disables_pack_reverse_index(tmp_path: Path) -> None:
    """Isolated Git config pins pack.writeReverseIndex=false for supported Git."""
    from yinshi.services.workspace_replica_materialization import _GIT_ENV

    repository = tmp_path / "config-probe"
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        check=True,
        capture_output=True,
    )
    completed = subprocess.run(
        ["git", "config", "pack.writeReverseIndex"],
        cwd=repository,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
    )

    assert completed.stdout.strip() == b"false"


def binding(role: str, media_type: str, artifact_id: str, content: bytes) -> ReplicaArtifactBinding:
    return ReplicaArtifactBinding(
        role=role,  # type: ignore[arg-type]
        media_type=media_type,
        artifact_id=artifact_id,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
    )


async def published_request(
    tmp_path: Path,
    *,
    operation_id: str = "0123456789abcdef0123456789abcdef",
    object_format: str = "sha1",
    head_state: str = "symbolic",
    head_target: bytes = b"refs/heads/devel",
    replace_ref: bool = False,
    conflict: bool = False,
    extra_entries: tuple[ManifestEntry, ...] = (),
) -> tuple[ReplicaMaterializationStore, ReplicaMaterializationRequest, Path, Path]:
    source = private_root(tmp_path / f"source-{operation_id[-4:]}")
    init = ["init", "-q", "--initial-branch=main"]
    if object_format == "sha256":
        init.append("--object-format=sha256")
    git(source, *init)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "plain.txt").write_bytes(b"committed\n")
    (source / "run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (source / "run.sh").chmod(0o755)
    git(source, "add", "plain.txt", "run.sh")
    git(source, "commit", "-qm", "initial")
    git(source, "branch", "topic")
    git(source, "tag", "-a", "v1", "-m", "version one")
    if replace_ref:
        original = git(source, "rev-parse", "HEAD").decode("ascii")
        git(source, "update-ref", f"refs/replace/{original}", original)
    refs = ["refs/heads/main", "refs/heads/topic", "refs/tags/v1"]
    if replace_ref:
        refs.append(f"refs/replace/{git(source, 'rev-parse', 'HEAD').decode('ascii')}")
    bundle = await create_committed_bundle(
        source,
        tuple(refs),
        include_head=head_state != "unborn",
    )

    (source / "plain.txt").write_bytes(b"staged\n")
    git(source, "add", "plain.txt")
    (source / "plain.txt").write_bytes(b"modified\n")
    (source / "untracked.bin").write_bytes(b"untracked\x00bytes")
    (source / ".gitignore").write_bytes(b"ignored.txt\n")
    (source / "ignored.txt").write_bytes(b"ignored\n")
    (source / "link").symlink_to("plain.txt")
    if conflict:
        conflict_oids = [
            git(source, "hash-object", "-w", "--stdin", input_bytes=value).decode("ascii")
            for value in (b"base", b"ours", b"theirs")
        ]
        index_info = b"".join(
            f"100644 {object_id} {stage}\tconflict.txt\n".encode("ascii")
            for stage, object_id in enumerate(conflict_oids, start=1)
        )
        git(source, "update-index", "--index-info", input_bytes=index_info)

    for entry in extra_entries:
        destination = source / os.fsdecode(entry.raw_path)
        if entry.kind is WorktreeEntryKind.FILE:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(entry.content)
        elif entry.kind is WorktreeEntryKind.SYMLINK:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(os.fsdecode(entry.content))
        else:
            destination.mkdir(parents=True, exist_ok=True)
    committed_head_oid = bytes.fromhex(git(source, "rev-parse", "HEAD").decode("ascii"))
    if head_state == "symbolic":
        head_target = b"refs/heads/main"
        head_oid = committed_head_oid
    elif head_state == "detached":
        head_target = b""
        head_oid = committed_head_oid
    else:
        # Model a real unborn source: HEAD points at a branch with no ref.
        git(source, "symbolic-ref", "HEAD", head_target.decode("ascii"))
        head_oid = b""
    entries = [
        ManifestEntry(
            raw_path=b".gitignore",
            kind=WorktreeEntryKind.FILE,
            content=b"ignored.txt\n",
        ),
        ManifestEntry(
            raw_path=b"ignored.txt",
            kind=WorktreeEntryKind.FILE,
            content=b"ignored\n",
            allowed_ignored=True,
        ),
        ManifestEntry(
            raw_path=b"link",
            kind=WorktreeEntryKind.SYMLINK,
            content=b"plain.txt",
        ),
        ManifestEntry(
            raw_path=b"plain.txt",
            kind=WorktreeEntryKind.FILE,
            content=b"modified\n",
        ),
        ManifestEntry(
            raw_path=b"run.sh",
            kind=WorktreeEntryKind.FILE,
            content=b"#!/bin/sh\nexit 0\n",
            executable=True,
        ),
        ManifestEntry(
            raw_path=b"untracked.bin",
            kind=WorktreeEntryKind.FILE,
            content=b"untracked\x00bytes",
        ),
    ]
    entries.extend(extra_entries)
    if conflict:
        entries.append(
            ManifestEntry(
                raw_path=b"conflict.txt",
                kind=WorktreeEntryKind.FILE,
                content=b"working conflict",
            )
        )
    index_oids = {
        bytes.fromhex(line.split()[1].decode("ascii"))
        for line in git(source, "ls-files", "--stage").splitlines()
    }
    root_oids = tuple(sorted(index_oids | ({head_oid} if head_oid else set())))
    worktree = encode_worktree_artifact(
        WorktreeArtifactInput(
            object_format=object_format,
            head_state=head_state,
            head_target=head_target,
            head_oid=head_oid,
            index_bytes=(source / ".git" / "index").read_bytes(),
            entries=entries,
            root_oids=root_oids,
            policy_digest=b"p" * 32,
        )
    )
    decoded_worktree = decode_worktree_artifact(worktree)
    objects = await create_index_object_pack(
        source,
        tuple(sorted({entry.object_id.hex() for entry in decoded_worktree.index_entries})),
        bundle.objects,
    )
    limits = ReplicaStoreLimits()
    declaration = ReplicaArtifactSetDeclaration(
        version=2,
        operation_id=operation_id,
        repository_id="repository-000000",
        workspace_id="workspace-0000000",
        identity=ReplicaIdentity("target-000000000", 1, "yinshi-executor-00"),
        object_format=object_format,  # type: ignore[arg-type]
        source_state_sha256=decoded_worktree.source_state_sha256.hex(),
        reconciliation_fingerprint="a" * 64,
        bundle=binding(
            "committed_bundle",
            "application/vnd.yinshi.git-bundle.v1",
            "bundle-000000000",
            bundle.bundle_bytes,
        ),
        worktree=binding(
            "worktree",
            "application/vnd.yinshi.replica-worktree.v1",
            "worktree-0000000",
            worktree,
        ),
        index_objects=binding(
            "index_objects",
            "application/vnd.yinshi.git-index-objects-pack.v1",
            "objects-00000000",
            objects.pack_bytes,
        ),
        limits=limits,
    )
    publications = private_root(tmp_path / f"publications-{operation_id[-4:]}")
    publication = WorkspaceReplicaPublicationStore(
        publications,
        ceilings=limits,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    receipt = await publication.publish_artifact_set(
        declaration,
        bundle.bundle_bytes,
        worktree,
        objects.pack_bytes,
    )
    staging = private_root(tmp_path / f"staging-{operation_id[-4:]}")
    journal = private_root(tmp_path / f"journal-{operation_id[-4:]}")
    store = ReplicaMaterializationStore(
        publication=publication,
        staging_root=staging,
        journal_root=journal,
        limits=limits,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    return store, ReplicaMaterializationRequest(1, declaration, receipt), staging, journal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("head_state", "object_format"),
    [("symbolic", "sha1"), ("detached", "sha1"), ("unborn", "sha1"), ("symbolic", "sha256")],
)
async def test_real_round_trip_and_exact_retry(
    tmp_path: Path, head_state: str, object_format: str
) -> None:
    store, request, staging, journal = await published_request(
        tmp_path, head_state=head_state, object_format=object_format
    )
    receipt = await store.materialize(request)
    final = staging / request.declaration.operation_id
    inode = final.stat().st_ino

    assert {path.name for path in final.iterdir()} == {"repo", "home"}
    assert not any((final / "home").iterdir())
    assert (final / "repo" / "plain.txt").read_bytes() == b"modified\n"
    assert (final / "repo" / "run.sh").stat().st_mode & 0o111
    assert os.readlink(final / "repo" / "link") == "plain.txt"
    assert git(final / "repo", "show-ref", "--verify", "refs/tags/v1")
    assert (final / "repo" / ".git" / "index").read_bytes() == (
        tmp_path / f"source-{request.declaration.operation_id[-4:]}" / ".git" / "index"
    ).read_bytes()
    assert await store.materialize(request) == receipt
    assert await store.reconcile(request) == receipt
    assert final.stat().st_ino == inode
    assert {path.name for path in journal.joinpath(request.declaration.operation_id).iterdir()} == {
        "intent.json",
        "stage.json",
        "prepared.json",
        "receipt.json",
    }


@pytest.mark.asyncio
async def test_unborn_conflict_index_and_supplemental_objects(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(
        tmp_path,
        head_state="unborn",
        conflict=True,
    )
    await store.materialize(request)
    repository = staging / request.declaration.operation_id / "repo"
    stages = git(repository, "ls-files", "--stage", "conflict.txt").splitlines()
    assert [line.split()[2] for line in stages] == [b"1", b"2", b"3"]
    assert git(repository, "symbolic-ref", "HEAD") == b"refs/heads/devel"


@pytest.mark.asyncio
async def test_collision_conflict_and_unjournaled_final_are_never_adopted(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(tmp_path)
    final = staging / request.declaration.operation_id
    final.mkdir(mode=0o700)
    (final / "sentinel").write_bytes(b"keep")
    with pytest.raises(ReplicaMaterializationCollisionError):
        await store.materialize(request)
    with pytest.raises(ReplicaMaterializationCollisionError):
        await store.reconcile(request)
    assert (final / "sentinel").read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_replace_refs_are_rejected_by_authoritative_bundle_decode(tmp_path: Path) -> None:
    # An end-to-end replace-ref request fails here, at authoritative upstream
    # bundle construction: committed bundle verification rejects every
    # advertised refs/replace/* ref. The materializer keeps a defensive
    # refs/replace/* rejection in _validate_source, but no downstream
    # end-to-end replace-ref flow can reach publication.
    with pytest.raises(GitError, match="replacement ref"):
        await published_request(tmp_path, replace_ref=True)


@pytest.mark.asyncio
async def test_reconcile_absent_returns_none(tmp_path: Path) -> None:
    store, request, _staging, _journal = await published_request(tmp_path)
    assert await store.reconcile(request) is None


@pytest.mark.asyncio
async def test_exact_concurrency_serializes_to_one_inode(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(tmp_path)
    first, second = await asyncio.gather(store.materialize(request), store.materialize(request))
    final = staging / request.declaration.operation_id
    assert first == second
    assert json.loads(first.operation_directory_identity)["inode"] == final.stat().st_ino


@pytest.mark.asyncio
async def test_final_same_inode_mutation_is_rejected(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(tmp_path)
    await store.materialize(request)
    final = staging / request.declaration.operation_id
    inode = final.stat().st_ino
    (final / "repo" / "plain.txt").write_bytes(b"tampered but same inode")
    with pytest.raises(ReplicaMaterializationRejectedError, match="digest"):
        await store.materialize(request)
    assert final.stat().st_ino == inode


def test_constructor_rejects_unsafe_or_aliased_roots(tmp_path: Path) -> None:
    limits = ReplicaStoreLimits()
    publication_root = private_root(tmp_path / "publications")
    publication = WorkspaceReplicaPublicationStore(
        publication_root,
        ceilings=limits,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    staging = private_root(tmp_path / "staging")
    journal = private_root(tmp_path / "journal")
    staging.chmod(0o755)
    with pytest.raises(ReplicaMaterializationRejectedError):
        ReplicaMaterializationStore(
            publication=publication,
            staging_root=staging,
            journal_root=journal,
            limits=limits,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )
    staging.chmod(0o700)
    with pytest.raises(ValueError, match="distinct"):
        ReplicaMaterializationStore(
            publication=publication,
            staging_root=staging,
            journal_root=staging,
            limits=limits,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )


@pytest.mark.asyncio
async def test_journal_mode_tampering_is_rejected(tmp_path: Path) -> None:
    store, request, _staging, journal = await published_request(tmp_path)
    await store.materialize(request)
    intent = journal / request.declaration.operation_id / "intent.json"
    intent.chmod(0o644)
    with pytest.raises(ReplicaMaterializationRejectedError, match="record identity"):
        await store.reconcile(request)


@pytest.mark.asyncio
async def test_staging_root_replacement_is_rejected(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(tmp_path)
    await store.materialize(request)
    moved = staging.with_name("moved-staging")
    staging.rename(moved)
    private_root(staging)
    with pytest.raises(ReplicaMaterializationRejectedError, match="root identity"):
        await store.materialize(request)
    assert (moved / request.declaration.operation_id).is_dir()


@pytest.mark.asyncio
async def test_nonblocking_cross_process_lock_fails_closed(tmp_path: Path) -> None:
    store, request, _staging, journal = await published_request(tmp_path)
    descriptor = os.open(journal, os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ReplicaMaterializationUnresolvedError, match="owns the journal"):
            await store.materialize(request)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@pytest.mark.asyncio
async def test_cancellation_after_intent_is_unresolved_then_reconciles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, staging, _journal = await published_request(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = module._build_pending

    async def blocked(*args: object, **kwargs: object) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(module, "_build_pending", blocked)
    task = asyncio.create_task(store.materialize(request))
    await entered.wait()
    task.cancel()
    with pytest.raises(ReplicaMaterializationUnresolvedError, match="cancelled"):
        await task
    monkeypatch.setattr(module, "_build_pending", original)
    receipt = await store.reconcile(request)
    assert receipt is not None
    assert (staging / request.declaration.operation_id).is_dir()


@pytest.mark.asyncio
async def test_rename_failure_is_unresolved_then_reconciles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, staging, _journal = await published_request(tmp_path)
    original = module.atomic_rename_no_replace
    failed = False

    def fail_final(source: Path, target: Path, **kwargs: object) -> None:
        nonlocal failed
        if target.name == request.declaration.operation_id and not failed:
            failed = True
            raise OSError(errno.EIO, "injected rename failure")
        original(source, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "atomic_rename_no_replace", fail_final)
    with pytest.raises(ReplicaMaterializationUnresolvedError, match="rename outcome"):
        await store.materialize(request)
    assert not (staging / request.declaration.operation_id).exists()
    monkeypatch.setattr(module, "atomic_rename_no_replace", original)
    receipt = await store.reconcile(request)
    assert receipt is not None


@pytest.mark.asyncio
async def test_journal_root_fsync_failure_leaves_retryable_empty_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, staging, journal = await published_request(tmp_path)
    original = module.os.fsync
    journal_inode = journal.stat().st_ino
    failed = False

    def fail_once(descriptor: int) -> None:
        nonlocal failed
        if os.fstat(descriptor).st_ino == journal_inode and not failed:
            failed = True
            raise OSError(errno.ENOSPC, "injected journal sync failure")
        original(descriptor)

    monkeypatch.setattr(module.os, "fsync", fail_once)
    with pytest.raises(ReplicaMaterializationUnresolvedError):
        await store.materialize(request)
    monkeypatch.setattr(module.os, "fsync", original)
    assert await store.reconcile(request) is None
    receipt = await store.materialize(request)
    assert receipt.operation_id == request.declaration.operation_id
    assert (staging / request.declaration.operation_id).is_dir()


@pytest.mark.asyncio
async def test_git_allowlist_and_offline_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, _staging, _journal = await published_request(tmp_path)
    original = module.run_git_bytes
    commands: list[str] = []
    environments: list[dict[str, str]] = []

    async def checked(arguments: list[str], *args: object, **kwargs: object) -> bytes:
        commands.append(arguments[0])
        environments.append(dict(kwargs.get("env") or {}))
        return await original(arguments, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "run_git_bytes", checked)
    await store.materialize(request)
    assert commands
    assert "ls-files" in commands  # bounded semantic index verification must run
    forbidden = {"clone", "fetch", "checkout", "reset", "submodule", "remote"}
    assert not forbidden & set(commands)
    assert all(environment["GIT_NO_REPLACE_OBJECTS"] == "1" for environment in environments)
    assert all(environment["GIT_NO_LAZY_FETCH"] == "1" for environment in environments)
    assert all(environment["GIT_OPTIONAL_LOCKS"] == "0" for environment in environments)
    assert all(environment["GIT_TERMINAL_PROMPT"] == "0" for environment in environments)


@pytest.mark.asyncio
async def test_materialized_tree_is_accepted_by_root_prepare_without_launcher_action(
    tmp_path: Path,
) -> None:
    store, request, staging, _journal = await published_request(tmp_path)
    await store.materialize(request)
    replicas = private_root(tmp_path / "replicas")
    replicas.chmod(0o711)
    runtime = private_root(tmp_path / "runtime")
    runtime.chmod(0o711)
    socket_pins = private_root(tmp_path / "socket-pins")
    quarantine = private_root(tmp_path / "quarantine")
    layout = LaunchLayout(
        staging_root=staging,
        replicas_root=replicas,
        runtime_root=runtime,
        socket_pins_root=socket_pins,
        executable=tmp_path / "unused-executor",
        systemd_run=tmp_path / "unused-systemd-run",
        quarantine_root=quarantine,
    )
    current_uid = os.getuid()
    current_gid = os.getgid()
    validator = ReplicaLifecycleValidator(
        layout=layout,
        root_uid=current_uid,
        root_gid=current_gid,
        broker_uid=current_uid,
        executor_uid=current_uid + 1,
        account_ids=lambda name: (
            (current_uid, current_gid)
            if name == "yinshi-broker"
            else (current_uid + 1, current_gid)
        ),
        validate_all_identities=False,
        unit_exists=lambda _unit: False,
        cgroup_processes=lambda _path: None,
        mount_id=lambda _descriptor: 1,
        transfer_fd_ownership=lambda _fd, _uid, _gid: None,
        transfer_entry_ownership=lambda *_args, **_kwargs: None,
        confirmed_owner=lambda _metadata, _uid: True,
    )
    with validator.prepared(request.declaration.operation_id) as locations:
        expected = replicas / request.declaration.operation_id
        assert locations.replica == expected / "repo"
        assert locations.home == expected / "home"


@pytest.mark.asyncio
async def test_real_unborn_topic_head_is_preserved_exactly(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(tmp_path, head_state="unborn")
    receipt = await store.materialize(request)
    repository = staging / request.declaration.operation_id / "repo"
    assert git(repository, "symbolic-ref", "HEAD") == b"refs/heads/devel"
    assert b"refs/heads/devel" not in git(repository, "show-ref")
    assert await store.materialize(request) == receipt


@pytest.mark.asyncio
async def test_nested_file_and_explicit_empty_directory_round_trip(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(
        tmp_path,
        extra_entries=(
            ManifestEntry(b"docs", WorktreeEntryKind.DIRECTORY),
            ManifestEntry(b"src", WorktreeEntryKind.DIRECTORY),
            ManifestEntry(b"src/main.py", WorktreeEntryKind.FILE, b"print('nested')\n"),
        ),
    )
    receipt = await store.materialize(request)
    repository = staging / request.declaration.operation_id / "repo"
    assert (repository / "src").is_dir()
    assert (repository / "src" / "main.py").read_bytes() == b"print('nested')\n"
    assert (repository / "docs").is_dir()
    assert not any((repository / "docs").iterdir())
    assert await store.materialize(request) == receipt


@pytest.mark.asyncio
async def test_final_replacement_during_source_recheck_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, staging, journal = await published_request(tmp_path)
    publication = store._publication
    original_recheck = publication.recheck_opened_published_artifact_set
    operation_id = request.declaration.operation_id
    swapped = False

    async def replacing_recheck(opened: object) -> None:
        nonlocal swapped
        final = staging / operation_id
        if final.exists() and not swapped:
            swapped = True
            final.rename(staging / ".foreign-kept")
            replacement = staging / operation_id
            replacement.mkdir(mode=0o700)
            (replacement / "repo").mkdir(mode=0o700)
            (replacement / "home").mkdir(mode=0o700)
        await original_recheck(opened)  # type: ignore[arg-type]

    monkeypatch.setattr(publication, "recheck_opened_published_artifact_set", replacing_recheck)
    with pytest.raises(ReplicaMaterializationRejectedError):
        await store.materialize(request)
    assert not (journal / operation_id / "receipt.json").exists()
    assert (staging / operation_id).is_dir()


@pytest.mark.asyncio
async def test_git_alternates_symlink_injection_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, _staging, journal = await published_request(tmp_path)
    original_git = module._git
    injected = False
    alternates_target = private_root(tmp_path / "empty-objects")

    async def injecting(arguments: list[str], *args: object, **kwargs: object) -> bytes:
        nonlocal injected
        result = await original_git(arguments, *args, **kwargs)  # type: ignore[arg-type]
        if not injected and arguments[:1] == ["bundle"]:
            injected = True
            info = Path(str(kwargs["cwd"])) / ".git" / "objects" / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "alternates").symlink_to(alternates_target)
        return result

    monkeypatch.setattr(module, "_git", injecting)
    with pytest.raises(ReplicaMaterializationRejectedError):
        await store.materialize(request)
    operation = journal / request.declaration.operation_id
    assert not (operation / "prepared.json").exists()
    assert not (operation / "receipt.json").exists()


@pytest.mark.asyncio
async def test_index_semantic_divergence_is_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, _staging, journal = await published_request(tmp_path)
    original_git = module._git

    async def diverging(arguments: list[str], *args: object, **kwargs: object) -> bytes:
        result = await original_git(arguments, *args, **kwargs)  # type: ignore[arg-type]
        if arguments[:1] == ["ls-files"]:
            result = result.replace(b"100644", b"100604", 1)
        return result

    monkeypatch.setattr(module, "_git", diverging)
    with pytest.raises(ReplicaMaterializationUnresolvedError):
        await store.materialize(request)
    operation = journal / request.declaration.operation_id
    assert not (operation / "prepared.json").exists()
    assert not (operation / "receipt.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_kind", "extra_entries"),
    [
        ("pending", ()),
        ("directory", (ManifestEntry(b"src", WorktreeEntryKind.DIRECTORY),)),
        ("file", ()),
    ],
)
async def test_changed_mount_id_is_rejected_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
    extra_entries: tuple[ManifestEntry, ...],
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, _staging, journal = await published_request(
        tmp_path,
        extra_entries=extra_entries,
    )
    original_build = module._build_pending
    changed_inode: int | None = None

    async def build_then_change(*args: object, **kwargs: object) -> None:
        nonlocal changed_inode
        await original_build(*args, **kwargs)  # type: ignore[arg-type]
        pending_path = args[0]
        assert isinstance(pending_path, Path)
        target = {
            "pending": pending_path,
            "directory": pending_path / "repo" / "src",
            "file": pending_path / "repo" / "plain.txt",
        }[target_kind]
        changed_inode = target.stat().st_ino

    def changed_mount_id(descriptor: int) -> int:
        metadata = os.fstat(descriptor)
        return metadata.st_dev + int(metadata.st_ino == changed_inode)

    monkeypatch.setattr(module, "_build_pending", build_then_change)
    monkeypatch.setattr(module, "_mount_id", changed_mount_id)
    with pytest.raises(ReplicaMaterializationRejectedError, match="mount"):
        await store.materialize(request)
    operation = journal / request.declaration.operation_id
    assert not (operation / "prepared.json").exists()
    assert not (operation / "receipt.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [0o400, 0o4700, 0o2600, 0o1600])
async def test_inexact_or_privileged_worktree_mode_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, _staging, journal = await published_request(tmp_path)
    original_git = module._git
    changed = False

    async def changing(arguments: list[str], *args: object, **kwargs: object) -> bytes:
        nonlocal changed
        result = await original_git(arguments, *args, **kwargs)  # type: ignore[arg-type]
        if arguments[:1] == ["ls-files"] and not changed:
            changed = True
            (Path(str(kwargs["cwd"])) / "plain.txt").chmod(mode)
        return result

    monkeypatch.setattr(module, "_git", changing)
    with pytest.raises(ReplicaMaterializationRejectedError, match="mode"):
        await store.materialize(request)
    operation = journal / request.declaration.operation_id
    assert not (operation / "prepared.json").exists()
    assert not (operation / "receipt.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        (Path("objects/info/alternates"), b"/tmp\n"),
        (Path("refs/heads/main.lock"), b"lock"),
        (Path("objects/pack/tmp_pack_bad"), b"temporary"),
    ],
)
async def test_forbidden_git_metadata_is_rejected_before_prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: Path,
    content: bytes,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, staging, journal = await published_request(tmp_path)
    original_git = module._git
    injected = False

    async def injecting(arguments: list[str], *args: object, **kwargs: object) -> bytes:
        nonlocal injected
        result = await original_git(arguments, *args, **kwargs)  # type: ignore[arg-type]
        if arguments[:1] == ["ls-files"] and not injected:
            injected = True
            target = Path(str(kwargs["cwd"])) / ".git" / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return result

    monkeypatch.setattr(module, "_git", injecting)
    with pytest.raises(ReplicaMaterializationRejectedError):
        await store.materialize(request)
    operation = journal / request.declaration.operation_id
    assert not (operation / "prepared.json").exists()
    assert not (operation / "receipt.json").exists()
    assert not (staging / request.declaration.operation_id).exists()


@pytest.mark.asyncio
async def test_reconstruction_disables_reflogs(tmp_path: Path) -> None:
    store, request, staging, _journal = await published_request(tmp_path)
    await store.materialize(request)
    assert not (staging / request.declaration.operation_id / "repo" / ".git" / "logs").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["journal-operation", "journal-record", "final"])
async def test_named_storage_links_have_deterministic_classification(
    tmp_path: Path,
    kind: str,
) -> None:
    store, request, staging, journal = await published_request(tmp_path)
    operation_id = request.declaration.operation_id
    target = private_root(tmp_path / f"foreign-{kind}")
    if kind == "journal-operation":
        (journal / operation_id).symlink_to(target)
        expected_error = ReplicaMaterializationRejectedError
    elif kind == "final":
        (staging / operation_id).symlink_to(target)
        expected_error = ReplicaMaterializationCollisionError
    else:
        await store.materialize(request)
        receipt = journal / operation_id / "receipt.json"
        receipt.unlink()
        receipt.symlink_to(target)
        expected_error = ReplicaMaterializationRejectedError
    with pytest.raises(expected_error):
        await store.materialize(request)


@pytest.mark.parametrize("failure_point", ["fstat", "stat", "mount"])
def test_mkdir_open_closes_descriptor_after_post_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    root = private_root(tmp_path / "mkdir-root")
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_fstat = module.os.fstat
    original_stat = module.os.stat
    original_mount_id = module._mount_id

    if failure_point == "fstat":

        def failing_fstat(descriptor: int) -> os.stat_result:
            if descriptor != parent:
                raise OSError("injected fstat failure")
            return original_fstat(descriptor)

        monkeypatch.setattr(module.os, "fstat", failing_fstat)
    elif failure_point == "stat":

        def failing_stat(*args: object, **kwargs: object) -> os.stat_result:
            if kwargs.get("dir_fd") == parent:
                raise OSError("injected stat failure")
            return original_stat(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(module.os, "stat", failing_stat)
    else:

        def failing_mount_id(descriptor: int) -> int:
            if descriptor != parent:
                raise RuntimeError("injected mount identity failure")
            return original_mount_id(descriptor)

        monkeypatch.setattr(module, "_mount_id", failing_mount_id)

    before = len(os.listdir("/dev/fd"))
    try:
        for _ in range(20):
            with pytest.raises((OSError, RuntimeError)):
                module._mkdir_open(
                    parent,
                    b"child",
                    uid=os.geteuid(),
                    device=original_fstat(parent).st_dev,
                    mount_id=original_mount_id(parent),
                )
        assert len(os.listdir("/dev/fd")) == before
    finally:
        os.close(parent)


@pytest.mark.asyncio
async def test_recorded_pending_link_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yinshi.services import workspace_replica_materialization as module

    store, request, staging, _journal = await published_request(tmp_path)
    target = private_root(tmp_path / "foreign-pending")
    original_publish = module._publish_record

    def publish_then_link(*args: object, **kwargs: object) -> None:
        original_publish(*args, **kwargs)  # type: ignore[arg-type]
        if args[2] == "intent.json":
            pending_name = args[3]["pending_name"]
            (staging / pending_name).symlink_to(target)

    monkeypatch.setattr(module, "_publish_record", publish_then_link)
    with pytest.raises(ReplicaMaterializationRejectedError):
        await store.materialize(request)
