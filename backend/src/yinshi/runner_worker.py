"""Stable runner-local configuration and restricted worker dispatcher factory."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from yinshi.services.github_app import GitHubCloneAccessResolver
from yinshi.tenant import TenantContext, get_user_db, init_user_db
from yinshi.worker_auth import WorkerPrincipal, prepare_worker_principal_storage
from yinshi.worker_runtime import WorkerHttpDispatcher

EnvironmentSetter = Callable[[str, str], None]

RunnerUserDataEncryptionMode = Literal["disabled", "required"]
_X25519_PRIVATE_KEY_LENGTH = 32
_PROMPT_EVENT_COUNT_MAX = 100_000


def validate_user_data_encryption_mode(
    value: object,
) -> RunnerUserDataEncryptionMode:
    """Return one supported runner user-data encryption mode."""
    if not isinstance(value, str):
        raise TypeError("user_data_encryption must be a string")
    if value == "disabled":
        return "disabled"
    if value == "required":
        return "required"
    raise ValueError("user_data_encryption must be disabled or required")


def _derive_worker_secrets(data_protection_key: bytes) -> tuple[str, str]:
    """Derive stable domain-separated application and field-encryption secrets."""
    if not isinstance(data_protection_key, bytes):
        raise TypeError("data_protection_key must be bytes")
    if len(data_protection_key) != _X25519_PRIVATE_KEY_LENGTH:
        raise ValueError("data_protection_key must contain exactly 32 bytes")
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=b"yinshi-runner-worker-storage-v1",
        info=b"runner-local database and field keys",
    ).derive(data_protection_key)
    secret_key = base64.urlsafe_b64encode(key_material[:32]).decode("ascii")
    encryption_pepper = key_material[32:].hex()
    assert len(secret_key) >= 32
    assert len(encryption_pepper) == 64
    return secret_key, encryption_pepper


def _derive_worker_bearer_root(data_protection_key: bytes) -> bytes:
    """Derive a domain-separated root from the persistent storage key."""
    if len(data_protection_key) != _X25519_PRIVATE_KEY_LENGTH:
        raise ValueError("data_protection_key must contain exactly 32 bytes")
    bearer_root = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"yinshi-runner-worker-bearer-root-v1",
        info=b"runner-local account bearer derivation",
    ).derive(data_protection_key)
    assert len(bearer_root) == 32
    return bearer_root


def _prepare_owner_directory(path: Path) -> None:
    """Create one real owner-only runner directory or reject unsafe storage."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise RuntimeError("runner worker data directory must be a real directory")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError("runner worker data directory must be owned by the runner user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError("runner worker data directory must have owner-only permissions")


def _bind_runner_account(binding_path: Path, user_id: str) -> None:
    """Persist an opaque owner-only account binding and compare it on restart."""
    expected_digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest().encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(binding_path, flags, 0o600)
    except FileExistsError:
        metadata = binding_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or binding_path.is_symlink():
            raise RuntimeError("runner account binding must be a real file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("runner account binding must be owner-only")
        stored_digest = binding_path.read_bytes()
        if len(stored_digest) != len(expected_digest):
            raise RuntimeError("runner account binding is corrupt")
        if not secrets.compare_digest(stored_digest, expected_digest):
            raise ValueError("runner worker cannot accept a different account")
        return

    try:
        written_bytes = 0
        while written_bytes < len(expected_digest):
            chunk_bytes = os.write(file_descriptor, expected_digest[written_bytes:])
            if chunk_bytes <= 0:
                raise RuntimeError("runner account binding write made no progress")
            written_bytes += chunk_bytes
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    metadata = binding_path.stat()
    if metadata.st_size != len(expected_digest):
        raise RuntimeError("runner account binding write was incomplete")


def _recover_interrupted_prompt_runs(tenant: TenantContext) -> None:
    """Preserve committed outcomes before interrupting orphaned worker runs."""
    from yinshi.services.prompt_journal import recover_prompt_database

    with get_user_db(tenant) as database:
        recover_prompt_database(database, reset_sessions=True)


class RunnerWorkerManager:
    """Create one account-bound worker app with stable encrypted local stores."""

    def __init__(
        self,
        *,
        data_directory: Path,
        data_protection_key: bytes,
        database_directory: Path | None = None,
        user_data_directory: Path | None = None,
        user_data_encryption: RunnerUserDataEncryptionMode = "disabled",
        environment_setter: EnvironmentSetter | None = None,
        github_clone_access_resolver: GitHubCloneAccessResolver | None = None,
    ) -> None:
        if github_clone_access_resolver is not None and not callable(github_clone_access_resolver):
            raise TypeError("github_clone_access_resolver must be callable")
        if not isinstance(data_directory, Path):
            raise TypeError("data_directory must be a pathlib.Path")
        if not data_directory.is_absolute():
            raise ValueError("runner worker data directory must be absolute")
        validated_user_data_encryption = validate_user_data_encryption_mode(user_data_encryption)
        storage_key = bytes(data_protection_key)
        secret_key, encryption_pepper = _derive_worker_secrets(storage_key)
        set_environment = environment_setter or os.environ.__setitem__
        if not callable(set_environment):
            raise TypeError("environment_setter must be callable")

        selected_database_directory = database_directory or data_directory
        selected_user_data_directory = user_data_directory or data_directory / "users"
        for selected_path, name in (
            (selected_database_directory, "database_directory"),
            (selected_user_data_directory, "user_data_directory"),
        ):
            if not isinstance(selected_path, Path) or not selected_path.is_absolute():
                raise ValueError(f"runner worker {name} must be an absolute pathlib.Path")
        _prepare_owner_directory(data_directory)
        import_directory = selected_user_data_directory / "imports"
        _prepare_owner_directory(selected_database_directory)
        _prepare_owner_directory(selected_user_data_directory)
        _prepare_owner_directory(import_directory)
        environment = {
            "ALLOWED_REPO_BASE": str(import_directory),
            "CONTAINER_ENABLED": "false",
            "CONTROL_DB_PATH": str(selected_database_directory / "control.db"),
            "CONTROL_FIELD_ENCRYPTION": "required",
            "DB_PATH": str(selected_database_directory / "legacy.db"),
            "DISABLE_AUTH": "true",
            "ENCRYPTION_PEPPER": encryption_pepper,
            "HOST": "127.0.0.1",
            "REQUIRE_HTTPS": "disabled",
            "SECRET_KEY": secret_key,
            "TENANT_DB_ENCRYPTION": "required",
            "TRUSTED_HOSTS": "localhost,127.0.0.1,[::1]",
            "USER_DATA_DIR": str(selected_user_data_directory),
            "USER_DATA_ENCRYPTION": validated_user_data_encryption,
        }
        for name, value in environment.items():
            set_environment(name, value)

        from yinshi.config import get_settings
        from yinshi.db import init_control_db, init_db

        get_settings.cache_clear()
        settings = get_settings()
        if settings.control_db_path != environment["CONTROL_DB_PATH"]:
            raise RuntimeError("runner worker control database configuration did not apply")
        if settings.user_data_dir != environment["USER_DATA_DIR"]:
            raise RuntimeError("runner worker user data configuration did not apply")
        if settings.user_data_encryption_mode != validated_user_data_encryption:
            raise RuntimeError("runner worker user data encryption configuration did not apply")
        init_control_db()
        init_db()

        self._data_directory = data_directory
        self._database_directory = selected_database_directory
        self._user_data_directory = selected_user_data_directory
        self._account_binding_path = data_directory / "account.binding"
        self._bearer_root = _derive_worker_bearer_root(storage_key)
        self._account_id: str | None = None
        self._dispatcher: WorkerHttpDispatcher | None = None
        self._github_clone_access_resolver = github_clone_access_resolver

    def dispatcher(self, user_id: str) -> WorkerHttpDispatcher:
        """Return the sole account dispatcher, rejecting cross-account capabilities."""
        if not isinstance(user_id, str) or not user_id or len(user_id) > 256:
            raise ValueError("runner worker user_id has an invalid length")
        if self._account_id is not None and not secrets.compare_digest(
            self._account_id,
            user_id,
        ):
            raise ValueError("runner worker cannot accept a different account")
        if self._dispatcher is not None:
            return self._dispatcher

        _bind_runner_account(self._account_binding_path, user_id)
        account_directory_name = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        account_data_prefix = self._user_data_directory / account_directory_name[:2]
        account_directory = account_data_prefix / account_directory_name
        _prepare_owner_directory(account_data_prefix)
        _prepare_owner_directory(account_directory)
        database_users_directory = self._database_directory / "users"
        account_database_prefix = database_users_directory / account_directory_name[:2]
        account_database_directory = account_database_prefix / account_directory_name
        _prepare_owner_directory(database_users_directory)
        _prepare_owner_directory(account_database_prefix)
        _prepare_owner_directory(account_database_directory)
        database_path = account_database_directory / "yinshi.db"
        bearer_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"yinshi-runner-worker-bearer-v1",
            info=user_id.encode("utf-8"),
        ).derive(self._bearer_root)
        bearer_token = base64.urlsafe_b64encode(bearer_key).rstrip(b"=").decode("ascii")
        principal = WorkerPrincipal(
            tenant=TenantContext(
                user_id=user_id,
                email=f"{account_directory_name[:16]}@runner.invalid",
                data_dir=str(account_directory),
                db_path=str(database_path),
            ),
            bearer_token=bearer_token,
            database_root=str(self._database_directory),
        )

        from yinshi.db import get_control_db
        from yinshi.main import create_app

        with get_control_db() as control_database:
            control_database.execute(
                """
                INSERT INTO users (id, email, status)
                VALUES (?, ?, 'active')
                ON CONFLICT(id) DO NOTHING
                """,
                (user_id, principal.tenant.email),
            )
            control_database.commit()
            user_row = control_database.execute(
                "SELECT email, status FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if user_row is None or user_row["email"] != principal.tenant.email:
            raise RuntimeError("runner worker account metadata conflicts with local storage")
        if user_row["status"] != "active":
            raise RuntimeError("runner worker account is not active")

        prepare_worker_principal_storage(principal)
        init_user_db(str(database_path), tenant=principal.tenant)
        _recover_interrupted_prompt_runs(principal.tenant)
        worker_app = create_app(mode="worker", worker_principal=principal)
        worker_app.state.container_manager = None
        if self._github_clone_access_resolver is not None:
            worker_app.state.github_clone_access_resolver = self._github_clone_access_resolver
        dispatcher = WorkerHttpDispatcher(app=worker_app, principal=principal)
        self._account_id = user_id
        self._dispatcher = dispatcher
        return dispatcher

    async def quiesce(self, job_id: str) -> None:
        """Close worker resources and checkpoint all runner-owned databases."""
        try:
            normalized_job_id = str(uuid.UUID(job_id))
        except (AttributeError, ValueError) as exc:
            raise ValueError("job_id must be a UUID") from exc
        if normalized_job_id != job_id:
            raise ValueError("job_id must be canonical")
        dispatcher = self._dispatcher
        if dispatcher is None:
            return
        prompt_journal = dispatcher.app.state.prompt_journal
        terminal_journal = dispatcher.app.state.terminal_journal
        await prompt_journal.close()
        await terminal_journal.close_all()
        from yinshi.db import get_control_db

        for connection_factory in (
            get_control_db,
            lambda: get_user_db(dispatcher.tenant),
        ):
            with connection_factory() as database:
                result = database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if result is None or len(result) != 3 or result[0] != 0:
                    raise RuntimeError("runner database checkpoint failed")
