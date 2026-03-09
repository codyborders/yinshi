"""Pydantic models for API request/response schemas."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RepoCreate(BaseModel):
    """Request to import a repository."""

    name: str
    remote_url: Optional[str] = None
    local_path: Optional[str] = None
    custom_prompt: Optional[str] = None


class RepoOut(BaseModel):
    """Repository response."""

    id: str
    created_at: datetime
    updated_at: datetime
    name: str
    remote_url: Optional[str] = None
    root_path: str
    custom_prompt: Optional[str] = None


class RepoUpdate(BaseModel):
    """Request to update a repository."""

    name: Optional[str] = None
    custom_prompt: Optional[str] = None


class WorkspaceCreate(BaseModel):
    """Request to create a worktree workspace."""

    name: Optional[str] = None


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
    content: Optional[str] = None
    full_message: Optional[str] = None
    turn_id: Optional[str] = None


class WSPrompt(BaseModel):
    """WebSocket message from client to send a prompt."""

    type: str = "prompt"
    prompt: str
    model: Optional[str] = None


class WSCancel(BaseModel):
    """WebSocket message from client to cancel."""

    type: str = "cancel"
