"""Shared model and provider catalog helpers."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SESSION_MODEL = "minimax/MiniMax-M2.7"

LEGACY_MODEL_ALIASES = {
    "haiku": "anthropic/claude-haiku-4-5-20251001",
    "minimax": DEFAULT_SESSION_MODEL,
    "minimax-m2.5-highspeed": "minimax/MiniMax-M2.5-highspeed",
    "minimax-m2.7": DEFAULT_SESSION_MODEL,
    "minimax-m2.7-highspeed": "minimax/MiniMax-M2.7-highspeed",
    "opus": "anthropic/claude-opus-4-20250514",
    "sonnet": "anthropic/claude-sonnet-4-20250514",
}


@dataclass(frozen=True, slots=True)
class ProviderSetupField:
    """Describe one provider-specific setup field."""

    key: str
    label: str
    required: bool
    secret: bool = False


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Metadata that Yinshi adds on top of the pi provider registry."""

    id: str
    label: str
    auth_strategies: tuple[str, ...]
    setup_fields: tuple[ProviderSetupField, ...]
    docs_url: str
    supported: bool = True


def _titleize_provider(provider_id: str) -> str:
    """Convert a provider identifier into a readable label."""
    if not isinstance(provider_id, str):
        raise TypeError("provider_id must be a string")
    normalized_provider_id = provider_id.strip()
    if not normalized_provider_id:
        raise ValueError("provider_id must not be empty")
    pieces = normalized_provider_id.replace("-", " ").split()
    return " ".join(piece.upper() if len(piece) <= 3 else piece.capitalize() for piece in pieces)


_COMMON_DOCS_URL = "https://www.npmjs.com/package/@mariozechner/pi-ai"

PROVIDER_METADATA_BY_ID: dict[str, ProviderMetadata] = {
    "anthropic": ProviderMetadata(
        id="anthropic",
        label="Anthropic",
        auth_strategies=("api_key", "oauth"),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "azure-openai-responses": ProviderMetadata(
        id="azure-openai-responses",
        label="Azure OpenAI",
        auth_strategies=("api_key_with_config",),
        setup_fields=(
            ProviderSetupField("baseUrl", "Base URL", False),
            ProviderSetupField("resourceName", "Resource Name", False),
            ProviderSetupField("azureDeploymentName", "Deployment Name", False),
            ProviderSetupField("apiVersion", "API Version", False),
        ),
        docs_url=_COMMON_DOCS_URL,
    ),
    "github-copilot": ProviderMetadata(
        id="github-copilot",
        label="GitHub Copilot",
        auth_strategies=("oauth",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "google-vertex": ProviderMetadata(
        id="google-vertex",
        label="Google Vertex AI",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "google": ProviderMetadata(
        id="google",
        label="Google",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "google-antigravity": ProviderMetadata(
        id="google-antigravity",
        label="Google Antigravity",
        auth_strategies=("oauth",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "google-gemini-cli": ProviderMetadata(
        id="google-gemini-cli",
        label="Google Gemini CLI",
        auth_strategies=("oauth",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "minimax": ProviderMetadata(
        id="minimax",
        label="MiniMax",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "minimax-cn": ProviderMetadata(
        id="minimax-cn",
        label="MiniMax China",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "mistral": ProviderMetadata(
        id="mistral",
        label="Mistral",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "openai": ProviderMetadata(
        id="openai",
        label="OpenAI",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "openai-codex": ProviderMetadata(
        id="openai-codex",
        label="OpenAI Codex",
        auth_strategies=("oauth",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "openrouter": ProviderMetadata(
        id="openrouter",
        label="OpenRouter",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "opencode": ProviderMetadata(
        id="opencode",
        label="OpenCode Zen",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "opencode-go": ProviderMetadata(
        id="opencode-go",
        label="OpenCode Go",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "vercel-ai-gateway": ProviderMetadata(
        id="vercel-ai-gateway",
        label="Vercel AI Gateway",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "groq": ProviderMetadata(
        id="groq",
        label="Groq",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "cerebras": ProviderMetadata(
        id="cerebras",
        label="Cerebras",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "xai": ProviderMetadata(
        id="xai",
        label="xAI",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "zai": ProviderMetadata(
        id="zai",
        label="ZAI",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "huggingface": ProviderMetadata(
        id="huggingface",
        label="Hugging Face",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    "kimi-coding": ProviderMetadata(
        id="kimi-coding",
        label="Kimi Coding",
        auth_strategies=("api_key",),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
    ),
    # Bedrock requires AWS SDK credential resolution rather than the auth.json-style
    # API key path that Yinshi currently supports per session.
    "amazon-bedrock": ProviderMetadata(
        id="amazon-bedrock",
        label="Amazon Bedrock",
        auth_strategies=(),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
        supported=False,
    ),
}


def get_provider_metadata(provider_id: str) -> ProviderMetadata:
    """Return metadata for a provider id, defaulting to unsupported."""
    if not isinstance(provider_id, str):
        raise TypeError("provider_id must be a string")
    normalized_provider_id = provider_id.strip()
    if not normalized_provider_id:
        raise ValueError("provider_id must not be empty")
    metadata = PROVIDER_METADATA_BY_ID.get(normalized_provider_id)
    if metadata is not None:
        return metadata
    return ProviderMetadata(
        id=normalized_provider_id,
        label=_titleize_provider(normalized_provider_id),
        auth_strategies=(),
        setup_fields=(),
        docs_url=_COMMON_DOCS_URL,
        supported=False,
    )


def normalize_model_ref(model: str | None) -> str:
    """Normalize stored or user-provided model values into canonical refs."""
    if model is None:
        return DEFAULT_SESSION_MODEL
    if not isinstance(model, str):
        raise TypeError("model must be a string or None")
    normalized_model = model.strip()
    if not normalized_model:
        raise ValueError("model must not be empty")
    canonical_model = LEGACY_MODEL_ALIASES.get(normalized_model.lower())
    if canonical_model is not None:
        return canonical_model
    return normalized_model
