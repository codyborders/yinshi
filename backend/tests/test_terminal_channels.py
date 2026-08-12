"""Tests for container lifetime around reconnectable terminal channels."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from yinshi.api import terminal_channels
from yinshi.services.terminal_journal import TerminalEventBatch
from yinshi.tenant import TenantContext


class FakeContainerManager:
    """Record container activity and lease calls in order."""

    def __init__(self, *, reservation_available: bool = True) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.leases: set[str] = set()
        self.reservation = object() if reservation_available else None

    async def acquire_activity(
        self,
        user_id: str,
        *,
        runtime_id: str | None = None,
    ) -> object | None:
        self.calls.append(("acquire", user_id, runtime_id))
        return self.reservation

    async def release_activity(self, reservation: object) -> None:
        self.calls.append(("release", reservation))

    def protect(
        self,
        user_id: str,
        lease_key: str,
        timeout_s: int,
        *,
        runtime_id: str | None = None,
    ) -> None:
        self.calls.append(("protect", user_id, lease_key, timeout_s, runtime_id))
        self.leases.add(lease_key)

    def unprotect(
        self,
        user_id: str,
        lease_key: str,
        *,
        runtime_id: str | None = None,
    ) -> None:
        self.calls.append(("unprotect", user_id, lease_key, runtime_id))
        self.leases.discard(lease_key)


class FakeTerminalJournal:
    """Record terminal operations without opening a sidecar socket."""

    def __init__(self, manager: FakeContainerManager) -> None:
        self.manager = manager
        self.calls: list[str] = []
        self.closed_events = False

    async def start(self, **_kwargs: object) -> str:
        assert self.manager.calls[-1][0] == "acquire"
        self.manager.calls.append(("journal-start",))
        return "terminal-id"

    async def input(self, **_kwargs: object) -> None:
        self.calls.append("input")

    async def resize(self, **_kwargs: object) -> None:
        self.calls.append("resize")

    async def restart(self, **_kwargs: object) -> None:
        self.calls.append("restart")

    async def events(self, **_kwargs: object) -> TerminalEventBatch:
        self.calls.append("events")
        return TerminalEventBatch(
            terminal_id="terminal-id",
            events=(),
            next_sequence=0,
            closed=self.closed_events,
        )

    async def close(self, **_kwargs: object) -> None:
        self.calls.append("close")


def _tenant() -> TenantContext:
    return TenantContext(
        user_id="user-id",
        email="user@example.com",
        data_dir="/tenant",
        db_path="/tenant/yinshi.db",
    )


def _request(manager: FakeContainerManager | None) -> Request:
    return Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(container_manager=manager),
            ),
        }
    )


@pytest.mark.asyncio
async def test_start_holds_exact_activity_until_lease_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start keeps one exact reservation through journal startup and lease install."""
    manager = FakeContainerManager()
    request = _request(manager)
    journal = FakeTerminalJournal(manager)
    tenant = _tenant()

    async def terminal_context(
        _request: Request,
        workspace_id: str,
    ) -> tuple[str, TenantContext, str, str, str]:
        return tenant.user_id, tenant, "/runtime.sock", "/workspace", workspace_id

    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(terminal_channels, "_terminal_context", terminal_context)
    monkeypatch.setattr(
        terminal_channels,
        "get_settings",
        lambda: SimpleNamespace(container_enabled=True, terminal_keepalive_s=321),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=True),
    )

    response = await terminal_channels.start_terminal_channel(
        "workspace-id",
        terminal_channels.TerminalStartRequest(cols=80, rows=24),
        request,
    )

    assert response.id == "terminal-id"
    assert manager.calls == [
        ("acquire", tenant.user_id, "workspace-id"),
        ("journal-start",),
        (
            "protect",
            tenant.user_id,
            "terminal:workspace-id:terminal-id",
            321,
            "workspace-id",
        ),
        ("release", manager.reservation),
    ]


@pytest.mark.asyncio
async def test_terminals_in_one_workspace_hold_distinct_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing one terminal retains the other terminal runtime lease."""
    manager = FakeContainerManager()
    request = _request(manager)
    journal = FakeTerminalJournal(manager)
    tenant = _tenant()
    terminal_ids = iter(("terminal-one", "terminal-two"))

    async def terminal_context(
        _request: Request,
        workspace_id: str,
    ) -> tuple[str, TenantContext, str, str, str]:
        return tenant.user_id, tenant, "/runtime.sock", "/workspace", workspace_id

    async def start_terminal(**_kwargs: object) -> str:
        manager.calls.append(("journal-start",))
        return next(terminal_ids)

    monkeypatch.setattr(journal, "start", start_terminal)
    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(terminal_channels, "_terminal_context", terminal_context)
    monkeypatch.setattr(
        terminal_channels,
        "_tenant_identity",
        lambda _request: (tenant.user_id, tenant),
    )
    monkeypatch.setattr(
        terminal_channels,
        "get_settings",
        lambda: SimpleNamespace(container_enabled=True, terminal_keepalive_s=321),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=True),
    )

    for _index in range(2):
        await terminal_channels.start_terminal_channel(
            "workspace-id",
            terminal_channels.TerminalStartRequest(cols=80, rows=24),
            request,
        )
    await terminal_channels.close_terminal_channel(
        "workspace-id",
        "terminal-one",
        request,
    )

    assert manager.leases == {"terminal:workspace-id:terminal-two"}


@pytest.mark.asyncio
async def test_start_failure_does_not_install_terminal_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed journal startup releases activity without installing a lease."""
    manager = FakeContainerManager()
    request = _request(manager)
    journal = FakeTerminalJournal(manager)
    tenant = _tenant()

    async def terminal_context(
        _request: Request,
        workspace_id: str,
    ) -> tuple[str, TenantContext, str, str, str]:
        return tenant.user_id, tenant, "/runtime.sock", "/workspace", workspace_id

    async def fail_start(**_kwargs: object) -> str:
        raise ConnectionError("sidecar unavailable")

    monkeypatch.setattr(journal, "start", fail_start)
    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(terminal_channels, "_terminal_context", terminal_context)
    monkeypatch.setattr(
        terminal_channels,
        "get_settings",
        lambda: SimpleNamespace(container_enabled=True, terminal_keepalive_s=321),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=True),
    )

    with pytest.raises(HTTPException) as raised:
        await terminal_channels.start_terminal_channel(
            "workspace-id",
            terminal_channels.TerminalStartRequest(cols=80, rows=24),
            request,
        )

    assert raised.value.status_code == 503
    assert manager.leases == set()
    assert manager.calls == [
        ("acquire", tenant.user_id, "workspace-id"),
        ("release", manager.reservation),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["input", "resize", "restart", "events"])
async def test_active_terminal_operations_refresh_workspace_lease(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Active terminal operations refresh the workspace runtime lease."""
    manager = FakeContainerManager()
    request = _request(manager)
    journal = FakeTerminalJournal(manager)
    tenant = _tenant()
    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(
        terminal_channels,
        "_tenant_identity",
        lambda _request: (tenant.user_id, tenant),
    )
    monkeypatch.setattr(
        terminal_channels,
        "get_settings",
        lambda: SimpleNamespace(container_enabled=True, terminal_keepalive_s=321),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=True),
    )

    if operation == "input":
        await terminal_channels.send_terminal_input(
            "workspace-id",
            "terminal-id",
            terminal_channels.TerminalInputRequest(data="pwd\r"),
            request,
        )
    elif operation == "resize":
        await terminal_channels.resize_terminal_channel(
            "workspace-id",
            "terminal-id",
            terminal_channels.TerminalResizeRequest(cols=80, rows=24),
            request,
        )
    elif operation == "restart":
        await terminal_channels.restart_terminal_channel(
            "workspace-id",
            "terminal-id",
            request,
        )
    else:
        await terminal_channels.get_terminal_events(
            "workspace-id",
            "terminal-id",
            0,
            request,
        )

    assert journal.calls == [operation]
    assert manager.calls == [
        ("acquire", tenant.user_id, "workspace-id"),
        (
            "protect",
            tenant.user_id,
            "terminal:workspace-id:terminal-id",
            321,
            "workspace-id",
        ),
        ("release", manager.reservation),
    ]


@pytest.mark.asyncio
async def test_closed_events_remove_workspace_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed event batch removes the workspace runtime lease."""
    manager = FakeContainerManager()
    request = _request(manager)
    journal = FakeTerminalJournal(manager)
    journal.closed_events = True
    tenant = _tenant()
    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(
        terminal_channels,
        "_tenant_identity",
        lambda _request: (tenant.user_id, tenant),
    )
    monkeypatch.setattr(
        terminal_channels,
        "get_settings",
        lambda: SimpleNamespace(container_enabled=True, terminal_keepalive_s=321),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=True),
    )

    response = await terminal_channels.get_terminal_events(
        "workspace-id",
        "terminal-id",
        0,
        request,
    )

    assert response.closed is True
    assert manager.calls == [
        ("acquire", tenant.user_id, "workspace-id"),
        (
            "unprotect",
            tenant.user_id,
            "terminal:workspace-id:terminal-id",
            "workspace-id",
        ),
        ("release", manager.reservation),
    ]


@pytest.mark.asyncio
async def test_explicit_close_removes_workspace_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit terminal close removes the workspace runtime lease."""
    manager = FakeContainerManager()
    request = _request(manager)
    journal = FakeTerminalJournal(manager)
    tenant = _tenant()
    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(
        terminal_channels,
        "_tenant_identity",
        lambda _request: (tenant.user_id, tenant),
    )
    monkeypatch.setattr(
        terminal_channels,
        "get_settings",
        lambda: SimpleNamespace(container_enabled=True, terminal_keepalive_s=321),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=True),
    )

    await terminal_channels.close_terminal_channel(
        "workspace-id",
        "terminal-id",
        request,
    )

    assert journal.calls == ["close"]
    assert manager.calls == [
        (
            "unprotect",
            tenant.user_id,
            "terminal:workspace-id:terminal-id",
            "workspace-id",
        ),
    ]


@pytest.mark.asyncio
async def test_missing_runtime_returns_fixed_unavailable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or retired runtime returns the fixed terminal 503 response."""
    manager = FakeContainerManager(reservation_available=False)
    request = _request(manager)
    journal = FakeTerminalJournal(manager)
    tenant = _tenant()
    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(
        terminal_channels,
        "_tenant_identity",
        lambda _request: (tenant.user_id, tenant),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=True),
    )

    with pytest.raises(HTTPException) as raised:
        await terminal_channels.send_terminal_input(
            "workspace-id",
            "terminal-id",
            terminal_channels.TerminalInputRequest(data="pwd\r"),
            request,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == "Terminal runtime unavailable"
    assert journal.calls == []
    assert manager.calls == [("acquire", tenant.user_id, "workspace-id")]


@pytest.mark.asyncio
async def test_no_container_mode_keeps_worker_terminal_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker and local modes still operate terminals without a container manager."""
    request = _request(None)
    manager = FakeContainerManager()
    journal = FakeTerminalJournal(manager)
    tenant = _tenant()
    monkeypatch.setattr(terminal_channels, "_journal", lambda _request: journal)
    monkeypatch.setattr(
        terminal_channels,
        "_tenant_identity",
        lambda _request: (tenant.user_id, tenant),
    )
    monkeypatch.setattr(
        terminal_channels,
        "get_settings",
        lambda: SimpleNamespace(container_enabled=False, terminal_keepalive_s=321),
    )
    monkeypatch.setattr(
        "yinshi.services.sidecar_runtime.get_settings",
        lambda: SimpleNamespace(container_enabled=False),
    )

    await terminal_channels.restart_terminal_channel(
        "workspace-id",
        "terminal-id",
        request,
    )

    assert journal.calls == ["restart"]
    assert manager.calls == []
