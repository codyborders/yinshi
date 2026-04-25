"""Pydantic models for API request/response schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from yinshi.model_catalog import DEFAULT_SESSION_MODEL, get_provider_metadata, normalize_model_ref

PI_CONFIG_CATEGORY_ORDER = (
    "skills",
    "extensions",
    "prompts",
    "agents",
    "themes",
    "settings",
    "models",
    "sessions",
    "instructions",
)
PI_CONFIG_CATEGORIES = frozenset(PI_CONFIG_CATEGORY_ORDER)


def _strip_required_text(value: str, message: str) -> str:
    """Trim a required string field and raise the caller's validation message."""
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value


def _strip_optional_text(value: str | None, message: str) -> str | None:
    """Trim an optional string field and reject explicit blank values."""
    if value is None:
        return None
    return _strip_required_text(value, message)


class RepoCreate(BaseModel):
    """Request to import a repository."""

    name: str = Field(..., max_length=255)
    remote_url: str | None = Field(None, max_length=2048)
    local_path: str | None = Field(None, max_length=4096)
    custom_prompt: str | None = Field(None, max_length=10_000)
    agents_md: str | None = Field(None, max_length=50_000)


class RepoOut(BaseModel):
    """Repository response."""

    id: str
    created_at: datetime
    updated_at: datetime
    name: str
    remote_url: str | None = None
    root_path: str
    custom_prompt: str | None = None
    agents_md: str | None = None


class RepoUpdate(BaseModel):
    """Request to update a repository."""

    name: str | None = Field(None, max_length=255)
    custom_prompt: str | None = Field(None, max_length=10_000)
    agents_md: str | None = Field(None, max_length=50_000)


class WorkspaceCreate(BaseModel):
    """Request to create a worktree workspace."""

    name: str | None = Field(None, max_length=255)


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


class WorkspaceUpdate(BaseModel):
    """Request to update a workspace."""

    state: str | None = Field(None, pattern=r"^(ready|archived)$")


class SessionCreate(BaseModel):
    """Request to create an agent session."""

    model: str = Field(DEFAULT_SESSION_MODEL, max_length=100)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Normalize session model values to canonical refs."""
        return normalize_model_ref(value)


class SessionUpdate(BaseModel):
    """Request to update a session."""

    model: str | None = Field(None, max_length=100)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        """Normalize optional session model values to canonical refs.

        Rejects explicit null so that PATCH cannot write NULL into the
        database, which would break SessionOut deserialization.
        """
        if value is None:
            raise ValueError("model cannot be set to null")
        return normalize_model_ref(value)


class SessionOut(BaseModel):
    """Session response."""

    id: str
    created_at: datetime
    updated_at: datetime
    workspace_id: str
    status: str = "idle"
    model: str = DEFAULT_SESSION_MODEL


class MessageOut(BaseModel):
    """Message response."""

    id: str
    created_at: datetime
    session_id: str
    role: str
    content: str | None = None
    full_message: str | None = None
    turn_id: str | None = None
    turn_status: str | None = None


class WSPrompt(BaseModel):
    """WebSocket message from client to send a prompt."""

    type: str = "prompt"
    prompt: str = Field(..., max_length=100_000)
    model: str | None = Field(None, max_length=100)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        """Normalize optional prompt model values to canonical refs."""
        if value is None:
            return None
        return normalize_model_ref(value)


class WSCancel(BaseModel):
    """WebSocket message from client to cancel."""

    type: str = "cancel"


# --- Multi-tenant models ---


class UserOut(BaseModel):
    """User account response (from control plane)."""

    id: str
    email: str
    display_name: str | None = None
    avatar_url: str | None = None
    status: str = "active"
    tier: str = "free"


class CloudRunnerCreate(BaseModel):
    """Request a one-time cloud runner registration token."""

    name: str = Field("AWS runner", min_length=1, max_length=120)
    cloud_provider: Literal["aws"] = "aws"
    region: str = Field("us-east-1", min_length=1, max_length=64)

    @field_validator("name", "region")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        """Reject blank runner setup fields after trimming whitespace."""
        return _strip_required_text(value, "Runner setup fields must not be blank")


class CloudRunnerOut(BaseModel):
    """Safe cloud runner status returned to the frontend."""

    id: str
    created_at: datetime
    updated_at: datetime
    name: str
    cloud_provider: str
    region: str
    status: Literal["pending", "online", "offline", "revoked"]
    registered_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    runner_version: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    data_dir: str | None = None


class CloudRunnerRegistrationOut(BaseModel):
    """Cloud runner registration response with the one-time token."""

    runner: CloudRunnerOut
    registration_token: str
    registration_token_expires_at: datetime
    control_url: str
    environment: dict[str, str]


class RunnerRegisterIn(BaseModel):
    """Registration payload submitted by a freshly bootstrapped runner."""

    registration_token: str = Field(..., min_length=32, max_length=512)
    runner_version: str = Field(..., min_length=1, max_length=120)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    data_dir: str = Field(..., min_length=1, max_length=4096)
    sqlite_dir: str | None = Field(None, min_length=1, max_length=4096)
    shared_files_dir: str | None = Field(None, min_length=1, max_length=4096)

    @field_validator("registration_token", "runner_version", "data_dir")
    @classmethod
    def validate_runner_registration_text(cls, value: str) -> str:
        """Reject blank runner registration strings."""
        return _strip_required_text(value, "Runner registration values must not be blank")

    @field_validator("sqlite_dir", "shared_files_dir")
    @classmethod
    def validate_runner_registration_path(cls, value: str | None) -> str | None:
        """Reject blank optional runner storage paths."""
        return _strip_optional_text(value, "Runner storage paths must not be blank")


class RunnerRegisterOut(BaseModel):
    """Registration response containing the runner's bearer token once."""

    runner_id: str
    runner_token: str
    status: Literal["online"] = "online"


class RunnerHeartbeatIn(BaseModel):
    """Heartbeat payload submitted by an already registered runner."""

    runner_version: str = Field(..., min_length=1, max_length=120)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    data_dir: str = Field(..., min_length=1, max_length=4096)
    sqlite_dir: str | None = Field(None, min_length=1, max_length=4096)
    shared_files_dir: str | None = Field(None, min_length=1, max_length=4096)

    @field_validator("runner_version", "data_dir")
    @classmethod
    def validate_runner_heartbeat_text(cls, value: str) -> str:
        """Reject blank runner heartbeat strings."""
        return _strip_required_text(value, "Runner heartbeat values must not be blank")

    @field_validator("sqlite_dir", "shared_files_dir")
    @classmethod
    def validate_runner_heartbeat_path(cls, value: str | None) -> str | None:
        """Reject blank optional runner storage paths."""
        return _strip_optional_text(value, "Runner storage paths must not be blank")


class RunnerHeartbeatOut(BaseModel):
    """Heartbeat acknowledgement returned to the runner."""

    runner_id: str
    status: Literal["online"] = "online"


class ProviderSetupFieldOut(BaseModel):
    """Describe one provider setup field to the frontend."""

    key: str
    label: str
    required: bool
    secret: bool = False


class ProviderDescriptorOut(BaseModel):
    """One provider in the catalog response."""

    id: str
    label: str
    auth_strategies: list[str]
    setup_fields: list[ProviderSetupFieldOut]
    docs_url: str
    connected: bool
    model_count: int


class ModelDescriptorOut(BaseModel):
    """One model in the catalog response."""

    ref: str
    provider: str
    id: str
    label: str
    api: str
    reasoning: bool
    inputs: list[str]
    context_window: int
    max_tokens: int


class ProviderCatalogOut(BaseModel):
    """Catalog response for providers and models."""

    default_model: str
    providers: list[ProviderDescriptorOut]
    models: list[ModelDescriptorOut]


class ProviderAuthStartOut(BaseModel):
    """Response returned when a provider OAuth flow starts."""

    flow_id: str
    provider: str
    auth_url: str
    instructions: str | None = None
    manual_input_required: bool = False
    manual_input_prompt: str | None = None
    manual_input_submitted: bool = False


class ProviderAuthStatusOut(BaseModel):
    """Status payload for a provider OAuth flow."""

    status: str
    provider: str
    flow_id: str
    instructions: str | None = None
    progress: list[str] = Field(default_factory=list)
    manual_input_required: bool = False
    manual_input_prompt: str | None = None
    manual_input_submitted: bool = False
    error: str | None = None


class ProviderAuthInputIn(BaseModel):
    """Manual OAuth input submitted from the browser UI."""

    flow_id: str = Field(..., min_length=1, max_length=255)
    authorization_input: str = Field(..., min_length=1, max_length=8192)

    @field_validator("flow_id")
    @classmethod
    def validate_flow_id(cls, value: str) -> str:
        """Reject blank OAuth flow identifiers."""
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("flow_id must not be empty")
        return normalized_value

    @field_validator("authorization_input")
    @classmethod
    def validate_authorization_input(cls, value: str) -> str:
        """Reject blank pasted OAuth callback input."""
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("authorization_input must not be empty")
        return normalized_value


class ProviderConnectionCreate(BaseModel):
    """Request to create a provider connection."""

    provider: str = Field(..., min_length=1, max_length=100)
    auth_strategy: str = Field(..., min_length=1, max_length=100)
    secret: str | dict[str, Any]
    label: str = Field("", max_length=255)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        """Reject blank providers."""
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("Provider must not be empty")
        return normalized_value

    @field_validator("auth_strategy")
    @classmethod
    def validate_auth_strategy(cls, value: str, info: ValidationInfo) -> str:
        """Require one of the provider's supported auth strategies."""
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("Auth strategy must not be empty")
        provider = info.data.get("provider")
        if isinstance(provider, str):
            metadata = get_provider_metadata(provider)
            if normalized_value not in metadata.auth_strategies:
                raise ValueError(f"{provider} does not support auth strategy {normalized_value}")
        return normalized_value

    @field_validator("secret")
    @classmethod
    def validate_secret(
        cls,
        value: str | dict[str, Any],
        info: ValidationInfo,
    ) -> str | dict[str, Any]:
        """Match secret shape to the requested auth strategy."""
        auth_strategy = info.data.get("auth_strategy")
        if auth_strategy == "api_key":
            if not isinstance(value, str):
                raise TypeError("API key connections require a string secret")
            normalized_value = value.strip()
            if not normalized_value:
                raise ValueError("API key secret must not be empty")
            return normalized_value
        if auth_strategy == "api_key_with_config":
            if isinstance(value, str):
                normalized_value = value.strip()
                if not normalized_value:
                    raise ValueError("API key secret must not be empty")
                return normalized_value
            if not isinstance(value, dict):
                raise TypeError("API key + config connections require a string or object secret")
            if not value:
                raise ValueError("API key + config secret must not be empty")
            return value
        if auth_strategy == "oauth":
            if not isinstance(value, dict):
                raise TypeError("OAuth connections require an object secret")
            if not value:
                raise ValueError("OAuth secret must not be empty")
            return value
        return value


class ProviderConnectionOut(BaseModel):
    """Provider connection response without the secret payload."""

    id: str
    created_at: datetime
    updated_at: datetime
    provider: str
    auth_strategy: str
    label: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    status: str
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    """Compatibility wrapper for legacy API-key endpoints."""

    provider: str = Field(..., min_length=1, max_length=100)
    key: str = Field(..., min_length=1, max_length=500)
    label: str = Field("", max_length=255)


class ApiKeyOut(BaseModel):
    """Compatibility wrapper for legacy API-key responses."""

    id: str
    created_at: datetime
    provider: str
    label: str = ""
    last_used_at: datetime | None = None


class PiConfigImport(BaseModel):
    """Import a Pi config from a GitHub repository."""

    repo_url: str = Field(..., max_length=2048)

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        """Reject blank repository URLs."""
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("Repository URL must not be empty")
        return normalized_value


class PiConfigCategoryUpdate(BaseModel):
    """Toggle the enabled Pi resource categories."""

    enabled_categories: list[str]

    @field_validator("enabled_categories")
    @classmethod
    def validate_enabled_categories(cls, value: list[str]) -> list[str]:
        """Require unique, known category names."""
        seen_categories: set[str] = set()
        normalized_categories: list[str] = []
        for category in value:
            normalized_category = category.strip()
            if normalized_category not in PI_CONFIG_CATEGORIES:
                raise ValueError(f"Unsupported category: {category}")
            if normalized_category in seen_categories:
                raise ValueError(f"Duplicate category: {category}")
            seen_categories.add(normalized_category)
            normalized_categories.append(normalized_category)
        return normalized_categories


class PiConfigOut(BaseModel):
    """Pi config status response."""

    id: str
    created_at: datetime
    updated_at: datetime
    source_type: str
    source_label: str
    last_synced_at: datetime | None = None
    status: str
    error_message: str | None = None
    available_categories: list[str]
    enabled_categories: list[str]


class PiCommand(BaseModel):
    """One slash command exposed from the user's imported Pi config.

    The ``kind`` discriminator preserves the source of the command so the UI
    can group or style entries, while keeping the wire format flat. Fields
    are the minimum needed to render + invoke the command; host filesystem
    paths and rarely-used metadata are intentionally omitted.
    """

    kind: Literal["skill", "prompt", "extension"]
    name: str
    description: str = ""
    command_name: str


class PiConfigCommandsOut(BaseModel):
    """Slash commands resolved from the imported Pi config.

    See sidecar/src/sidecar.js listResources for the producer side. The
    wire contract is a single flat list; the ``kind`` field on each entry
    distinguishes skills, prompts, and extension-registered commands.
    """

    commands: list[PiCommand] = Field(default_factory=list)


class PiPackageUpdateStatusOut(BaseModel):
    """Last recorded result from the daily pi package updater."""

    checked_at: str | None = None
    status: str | None = None
    previous_version: str | None = None
    current_version: str | None = None
    latest_version: str | None = None
    updated: bool | None = None
    message: str | None = None


class PiPackageReleaseOut(BaseModel):
    """One upstream pi release note entry."""

    tag_name: str
    version: str
    name: str
    published_at: str | None = None
    html_url: str
    body_markdown: str


class PiReleaseNotesOut(BaseModel):
    """Runtime pi version plus recent upstream release notes."""

    package_name: str
    installed_version: str | None = None
    latest_version: str | None = None
    node_version: str | None = None
    release_notes_url: str
    update_schedule: str
    update_status: PiPackageUpdateStatusOut | None = None
    runtime_error: str | None = None
    release_error: str | None = None
    releases: list[PiPackageReleaseOut] = Field(default_factory=list)
