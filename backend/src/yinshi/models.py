"""Pydantic models for API request/response schemas."""

from datetime import datetime

from pydantic import BaseModel


class RepoCreate(BaseModel):
    """Request to import a repository."""

    name: str
    remote_url: str | None = None
    local_path: str | None = None
    custom_prompt: str | None = None


class RepoOut(BaseModel):
    """Repository response."""

    id: str
    created_at: datetime
    updated_at: datetime
    name: str
    remote_url: str | None = None
    root_path: str
    custom_prompt: str | None = None
    owner_email: str | None = None


class RepoUpdate(BaseModel):
    """Request to update a repository."""

    name: str | None = None
    custom_prompt: str | None = None


class WorkspaceCreate(BaseModel):
    """Request to create a worktree workspace."""

    name: str | None = None


class WorkspaceOut(BaseModel):
    """Workspace response."""

    id: str
    created_at: datetime
    updated_at: datetime
    repo_id: str
    name: str
    branch: str
    path: str
    state: str = "ready"


class SessionCreate(BaseModel):
    """Request to create an agent session."""

    model: str = "minimax"


class SessionOut(BaseModel):
    """Session response."""

    id: str
    created_at: datetime
    updated_at: datetime
    workspace_id: str
    status: str = "idle"
    model: str = "minimax"


class MessageOut(BaseModel):
    """Message response."""

    id: str
    created_at: datetime
    session_id: str
    role: str
    content: str | None = None
    full_message: str | None = None
    turn_id: str | None = None


class WSPrompt(BaseModel):
    """WebSocket message from client to send a prompt."""

    type: str = "prompt"
    prompt: str
    model: str | None = None


class WSCancel(BaseModel):
    """WebSocket message from client to cancel."""

    type: str = "cancel"
