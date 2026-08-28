"""Canonical managed backup guest-result contract shared by manager and guest."""

from __future__ import annotations

import json

RESTORE_RESULT_STATUS = "restored"
RESTORE_RESULT_FIELDS = frozenset({"cleanup_pending", "job_id", "status"})


def parse_managed_restore_result(payload: bytes | str, *, job_id: str) -> bool:
    """Validate one restore result and report whether journal cleanup is pending.

    A committed restore reports ``cleanup_pending`` as a Boolean. Both values
    describe a durable commit; ``True`` only means rollback-journal removal has
    not finished and remains retryable.
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("managed restore guest result is invalid") from None
    if not isinstance(payload, str):
        raise ValueError("managed restore guest result is invalid")
    try:
        result = json.loads(payload)
    except json.JSONDecodeError:
        raise ValueError("managed restore guest result is invalid") from None
    if (
        not isinstance(result, dict)
        or set(result) != RESTORE_RESULT_FIELDS
        or result.get("job_id") != job_id
        or result.get("status") != RESTORE_RESULT_STATUS
        or type(result.get("cleanup_pending")) is not bool
    ):
        raise ValueError("managed restore guest result is invalid")
    return bool(result["cleanup_pending"])
