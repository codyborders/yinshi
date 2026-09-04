"""Delegation ID shape validation for thread workspaces."""

from __future__ import annotations

import asyncio

import pytest

from yinshi.services.thread_workspaces import ThreadWorkspaceService

VALID_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "short",
        "D4E5F6A7B8C9D0E1F2A3B4C5D6E7F801",
        "zzzzzzzzb8c9d0e1f2a3b4c5d6e7f801",
        VALID_ID + "0",
    ],
)
def test_malformed_delegation_ids_are_rejected(bad_id, db):
    """Only lowercase 32-hex delegation IDs derive refs or branches."""
    service = ThreadWorkspaceService()

    with pytest.raises(ValueError, match="delegation_id"):
        asyncio.run(
            service.provision_child(
                db,
                None,
                parent_workspace_id="parent-ws",
                delegation_id=bad_id,
            )
        )

    with pytest.raises(ValueError, match="delegation_id"):
        asyncio.run(
            service.discard_partial_child(
                db,
                None,
                delegation_id=bad_id,
                workspace_id=None,
            )
        )

    with pytest.raises(ValueError, match="delegation_id"):
        asyncio.run(
            service.finalize_child(
                db,
                None,
                delegation_id=bad_id,
                workspace_id="ws",
                base_commit=VALID_ID,
            )
        )
