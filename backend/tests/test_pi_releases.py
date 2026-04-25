"""Tests for pi package release-note and updater-status endpoints.

The tests mock both external boundaries: GitHub release fetching and the live
sidecar socket. This keeps the Settings tab contract stable without depending
on network access or a running Node sidecar during pytest.
"""

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient


class FakeVersionSidecar:
    """Small async fake for the sidecar version RPC."""

    def __init__(self, payload: dict[str, str]) -> None:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary")
        if not payload:
            raise ValueError("payload must not be empty")
        self.payload = payload
        self.disconnected = False

    async def get_runtime_version(self) -> dict[str, str]:
        """Return the configured sidecar runtime version payload."""
        return self.payload

    async def disconnect(self) -> None:
        """Record cleanup so the service can call it unconditionally."""
        self.disconnected = True


def _reset_release_cache(monkeypatch) -> None:
    """Clear module-level release cache so tests cannot affect each other."""
    import yinshi.services.pi_releases as pi_releases

    monkeypatch.setattr(pi_releases, "_release_cache_payload", None)
    monkeypatch.setattr(pi_releases, "_release_cache_expires_at", 0.0)


def test_pi_release_notes_route_returns_runtime_and_releases(
    auth_client: TestClient,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The endpoint should merge runtime, updater, and release data."""
    import yinshi.services.pi_releases as pi_releases
    from yinshi.config import get_settings

    _reset_release_cache(monkeypatch)
    status_path = tmp_path / "pi-package-update.json"
    status_path.write_text(
        """
        {
          "checked_at": "2026-04-25T04:30:00Z",
          "status": "updated",
          "previous_version": "0.63.1",
          "current_version": "0.70.2",
          "latest_version": "0.70.2",
          "updated": true,
          "message": "Updated pi package"
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("PI_UPDATE_STATUS_PATH", str(status_path))
    get_settings.cache_clear()

    async def fake_connect() -> FakeVersionSidecar:
        return FakeVersionSidecar(
            {
                "package_name": "@mariozechner/pi-coding-agent",
                "installed_version": "0.70.2",
                "node_version": "v20.20.1",
            }
        )

    async def fake_fetch_releases(repository: str) -> dict[str, Any]:
        assert repository == "badlogic/pi-mono"
        release_prefix = "https://github.com/badlogic/pi-mono"
        release_url = f"{release_prefix}/releases/tag/v0.70.2"
        body_markdown = "### Fixed\n\n- Fixed provider retry controls."
        return {
            "latest_version": "0.70.2",
            "releases": [
                {
                    "tag_name": "v0.70.2",
                    "version": "0.70.2",
                    "name": "v0.70.2",
                    "published_at": "2026-04-24T12:21:42Z",
                    "html_url": release_url,
                    "body_markdown": body_markdown,
                }
            ],
        }

    monkeypatch.setattr(
        pi_releases,
        "create_sidecar_connection",
        fake_connect,
    )
    monkeypatch.setattr(
        pi_releases,
        "_fetch_github_releases",
        fake_fetch_releases,
    )

    response = auth_client.get("/api/settings/pi-release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["package_name"] == "@mariozechner/pi-coding-agent"
    assert body["installed_version"] == "0.70.2"
    assert body["latest_version"] == "0.70.2"
    assert body["node_version"] == "v20.20.1"
    assert body["update_status"]["status"] == "updated"
    assert body["update_status"]["updated"] is True
    assert body["releases"][0]["body_markdown"].startswith("### Fixed")
    assert body["runtime_error"] is None
    assert body["release_error"] is None


async def test_get_pi_release_notes_reports_fetch_failures(
    monkeypatch,
) -> None:
    """Release-note failures should not hide runtime version information."""
    import yinshi.services.pi_releases as pi_releases

    _reset_release_cache(monkeypatch)

    async def fake_connect() -> FakeVersionSidecar:
        return FakeVersionSidecar(
            {
                "package_name": "@mariozechner/pi-coding-agent",
                "installed_version": "0.70.2",
                "node_version": "v20.20.1",
            }
        )

    async def failing_fetch_releases(repository: str) -> dict[str, Any]:
        assert repository == "badlogic/pi-mono"
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(
        pi_releases,
        "create_sidecar_connection",
        fake_connect,
    )
    monkeypatch.setattr(
        pi_releases,
        "_fetch_github_releases",
        failing_fetch_releases,
    )

    payload = await pi_releases.get_pi_release_notes()

    assert payload["installed_version"] == "0.70.2"
    assert payload["latest_version"] is None
    assert payload["releases"] == []
    assert payload["release_error"] == "network down"
