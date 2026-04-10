"""Runtime git authentication helpers for sidecar-backed agent sessions."""

from __future__ import annotations

from dataclasses import dataclass

from yinshi.services.github_app import resolve_github_runtime_access_token


@dataclass(frozen=True, slots=True)
class GitRuntimeAuth:
    """One short-lived git credential payload for the sidecar."""

    strategy: str
    host: str
    access_token: str

    def as_sidecar_payload(self) -> dict[str, object]:
        """Serialize the runtime credential into the sidecar wire format."""
        if self.strategy != "github_app_https":
            raise ValueError(f"Unsupported git auth strategy: {self.strategy}")
        if self.host != "github.com":
            raise ValueError(f"Unsupported git auth host: {self.host}")
        if not self.access_token:
            raise ValueError("access_token must not be empty")
        return {
            "strategy": self.strategy,
            "host": self.host,
            "accessToken": self.access_token,
        }


async def resolve_git_runtime_auth(
    user_id: str | None,
    remote_url: str | None,
    installation_id: int | None,
) -> GitRuntimeAuth | None:
    """Resolve one ephemeral git credential for the current prompt session."""
    if user_id is None:
        return None
    if not isinstance(user_id, str):
        raise TypeError("user_id must be a string or None")
    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise ValueError("user_id must not be empty")
    if remote_url is None:
        return None
    if not isinstance(remote_url, str):
        raise TypeError("remote_url must be a string or None")
    normalized_remote_url = remote_url.strip()
    if not normalized_remote_url:
        return None
    if installation_id is not None and not isinstance(installation_id, int):
        raise TypeError("installation_id must be an integer or None")
    if installation_id is not None and installation_id <= 0:
        raise ValueError("installation_id must be positive")

    access_token = await resolve_github_runtime_access_token(
        normalized_user_id,
        normalized_remote_url,
        installation_id,
    )
    if access_token is None:
        return None
    return GitRuntimeAuth(
        strategy="github_app_https",
        host="github.com",
        access_token=access_token,
    )
