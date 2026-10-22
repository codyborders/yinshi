"""Tests for private replacement runner registration identity."""

from __future__ import annotations


def test_restore_candidate_authority_is_bound_to_exact_job(auth_client) -> None:
    """Candidate lookup and revocation should require the restore job identity."""
    from yinshi.services.runners import (
        create_managed_restore_registration,
        get_managed_restore_runner_for_job,
        revoke_managed_restore_runner_for_job,
    )

    tenant = getattr(auth_client, "yinshi_tenant")
    registration = create_managed_restore_registration(
        tenant.user_id,
        job_id="job-1",
        name="Managed restore candidate",
        region="ord",
        control_url="https://control.example",
    )

    assert get_managed_restore_runner_for_job(tenant.user_id, "job-1")["id"] == (
        registration["runner"]["id"]
    )
    assert get_managed_restore_runner_for_job(tenant.user_id, "job-2") is None
    assert not revoke_managed_restore_runner_for_job(tenant.user_id, "job-2")
    assert revoke_managed_restore_runner_for_job(tenant.user_id, "job-1")
