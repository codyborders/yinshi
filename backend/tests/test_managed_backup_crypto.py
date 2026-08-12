"""Tests for sealed managed-backup maintenance jobs."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_sealed_job_roundtrips_for_the_intended_runner() -> None:
    """The runner private key should open one job without exposing plaintext."""
    from yinshi.services.managed_backup_crypto import (
        open_managed_backup_job,
        seal_managed_backup_job,
    )

    runner_private_key = X25519PrivateKey.generate()
    runner_public_key = _base64url(runner_private_key.public_key().public_bytes_raw())
    payload = {
        "archive_id": "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e6f",
        "archive_key": _base64url(b"k" * 32),
        "operation": "create",
        "runtime_generation": 7,
    }

    envelope = seal_managed_backup_job(
        payload,
        runner_public_key=runner_public_key,
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70",
    )

    assert b"archive_key" not in envelope
    assert _base64url(b"k" * 32).encode("ascii") not in envelope
    assert (
        open_managed_backup_job(
            envelope,
            runner_private_key=runner_private_key.private_bytes_raw(),
            expected_job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70",
        )
        == payload
    )


def test_sealed_job_rejects_another_runner_or_job_identifier() -> None:
    """Identity and job binding should reject copied maintenance instructions."""
    from yinshi.services.managed_backup_crypto import (
        open_managed_backup_job,
        seal_managed_backup_job,
    )

    intended_runner = X25519PrivateKey.generate()
    other_runner = X25519PrivateKey.generate()
    envelope = seal_managed_backup_job(
        {"operation": "create"},
        runner_public_key=_base64url(intended_runner.public_key().public_bytes_raw()),
        job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70",
    )

    with pytest.raises(ValueError, match="could not be opened"):
        open_managed_backup_job(
            envelope,
            runner_private_key=other_runner.private_bytes_raw(),
            expected_job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e70",
        )
    with pytest.raises(ValueError, match="could not be opened"):
        open_managed_backup_job(
            envelope,
            runner_private_key=intended_runner.private_bytes_raw(),
            expected_job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e71",
        )


def test_sealed_job_can_be_opened_by_a_fresh_process(tmp_path) -> None:
    """A sealed job must carry ciphertext rather than depend on process memory."""
    import json
    import subprocess
    import sys

    from yinshi.services.managed_backup_crypto import seal_managed_backup_job

    runner_private_key = X25519PrivateKey.generate()
    payload = {"archive_key": _base64url(b"z" * 32), "operation": "restore"}
    envelope_path = tmp_path / "job.enc"
    private_key_path = tmp_path / "runner.key"
    envelope_path.write_bytes(
        seal_managed_backup_job(
            payload,
            runner_public_key=_base64url(runner_private_key.public_key().public_bytes_raw()),
            job_id="018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e72",
        )
    )
    private_key_path.write_bytes(runner_private_key.private_bytes_raw())

    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,pathlib,sys;"
                "from yinshi.services.managed_backup_crypto import open_managed_backup_job;"
                "print(json.dumps(open_managed_backup_job("
                "pathlib.Path(sys.argv[1]).read_bytes(),"
                "runner_private_key=pathlib.Path(sys.argv[2]).read_bytes(),"
                "expected_job_id=sys.argv[3]),sort_keys=True))"
            ),
            str(envelope_path),
            str(private_key_path),
            "018f47a2-9d3a-7f3b-8f0f-1a2b3c4d5e72",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == json.dumps(payload, sort_keys=True)


def test_archive_key_wrap_binds_owner_archive_and_key_identifier() -> None:
    """Stored key envelopes should fail outside their exact archive context."""
    from yinshi.services.managed_backup_crypto import (
        unwrap_managed_archive_key,
        wrap_managed_archive_key,
    )

    archive_key = b"a" * 32
    wrapping_key = b"w" * 32
    envelope = wrap_managed_archive_key(
        archive_key,
        user_id="user-1",
        archive_id="archive-1",
        key_id="backup-key-v1",
        wrapping_key=wrapping_key,
    )

    assert archive_key not in envelope
    assert (
        unwrap_managed_archive_key(
            envelope,
            user_id="user-1",
            archive_id="archive-1",
            keyring={"backup-key-v1": wrapping_key},
        )
        == archive_key
    )
    with pytest.raises(ValueError, match="could not be unwrapped"):
        unwrap_managed_archive_key(
            envelope,
            user_id="user-2",
            archive_id="archive-1",
            keyring={"backup-key-v1": wrapping_key},
        )
    with pytest.raises(ValueError, match="could not be unwrapped"):
        unwrap_managed_archive_key(
            envelope,
            user_id="user-1",
            archive_id="archive-2",
            keyring={"backup-key-v1": wrapping_key},
        )
