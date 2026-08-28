"""Tests for the canonical managed restore-result contract."""

from __future__ import annotations

import pytest

from yinshi.services.managed_backup_protocol import parse_managed_restore_result


def test_parse_managed_restore_result_contract() -> None:
    """The shared rule accepts committed Boolean states and rejects every drift."""
    valid_false = '{"cleanup_pending":false,"job_id":"job-1","status":"restored"}'
    valid_true = '{"cleanup_pending":true,"job_id":"job-1","status":"restored"}'

    assert parse_managed_restore_result(valid_false, job_id="job-1") is False
    assert parse_managed_restore_result(valid_true, job_id="job-1") is True
    assert parse_managed_restore_result(valid_true.encode("ascii"), job_id="job-1") is True

    invalid_payloads = [
        '{"cleanup_pending":"true","job_id":"job-1","status":"restored"}',
        '{"cleanup_pending":1,"job_id":"job-1","status":"restored"}',
        '{"cleanup_pending":null,"job_id":"job-1","status":"restored"}',
        '{"cleanup_pending":true,"job_id":"job-2","status":"restored"}',
        '{"cleanup_pending":true,"job_id":"job-1","status":"restoring"}',
        '{"cleanup_pending":true,"job_id":"job-1","status":"restored","extra":1}',
        '{"cleanup_pending":true,"job_id":"job-1"}',
        "null",
        "not-json",
        b'{"cleanup_pending":true,"job_id":"job-1","status":"restored"}\xff',
        7,
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValueError, match="managed restore guest result is invalid"):
            parse_managed_restore_result(payload, job_id="job-1")
