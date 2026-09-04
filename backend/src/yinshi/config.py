"""Application configuration via environment variables."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

_SECURITY_MODE_VALUES = {"auto", "disabled", "enabled", "required"}
_MANAGED_RUNTIME_PROVIDER_VALUES = {"disabled", "fly_sprites"}
_MANAGED_BACKUP_PROVIDER_VALUES = {"aws_s3", "digitalocean_spaces"}
_DNS_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SPRITE_PREFIX_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _generate_secret() -> str:
    return secrets.token_hex(32)


def _decode_hex_secret(value: str, name: str) -> bytes:
    """Decode a hex secret and reject values too weak for AES-256 use."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized_value = value.strip()
    if not normalized_value:
        return b""
    try:
        decoded_value = bytes.fromhex(normalized_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a valid hex string: {exc}") from exc
    if len(decoded_value) < 32:
        raise RuntimeError(f"{name} must be at least 32 bytes (64 hex characters)")
    return decoded_value


def _require_https_url(
    value: str,
    name: str,
    *,
    reject_routing_metadata: bool = False,
) -> None:
    """Require a safe absolute HTTPS URL."""
    parsed_url = urlsplit(value)
    try:
        port = parsed_url.port
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a complete HTTPS URL") from exc
    invalid_routing_metadata = reject_routing_metadata and (
        "?" in value or "#" in value or port not in {None, 443}
    )
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or any(character.isspace() for character in value)
        or invalid_routing_metadata
    ):
        raise RuntimeError(f"{name} must be a complete HTTPS URL")


def _validate_allowed_domains(value: str) -> None:
    """Require unique lowercase public DNS names with optional leading wildcards."""
    if not value:
        return
    if any(character.isspace() for character in value):
        raise RuntimeError("SPRITES_ALLOWED_DOMAINS must not contain whitespace")
    entries = value.split(",")
    if len(entries) != len(set(entries)):
        raise RuntimeError("SPRITES_ALLOWED_DOMAINS must not contain duplicates")
    for entry in entries:
        if entry == "*":
            raise RuntimeError("SPRITES_ALLOWED_DOMAINS must not contain a global wildcard")
        domain = entry[2:] if entry.startswith("*.") else entry
        if "*" in domain:
            raise RuntimeError("SPRITES_ALLOWED_DOMAINS contains an invalid wildcard")
        labels = domain.split(".")
        if (
            len(domain) > 253
            or len(labels) < 2
            or any(_DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels)
            or labels[-1].isdigit()
        ):
            raise RuntimeError("SPRITES_ALLOWED_DOMAINS must contain public DNS names")
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            continue
        raise RuntimeError("SPRITES_ALLOWED_DOMAINS must not contain IP literals")


def _control_domain_is_allowed(control_hostname: str, allowed_domains: str) -> bool:
    """Return whether an allowed-domain entry covers the public control host."""
    for entry in allowed_domains.split(","):
        if entry == control_hostname:
            return True
        if entry.startswith("*.") and control_hostname.endswith(f".{entry[2:]}"):
            return True
    return False


def _normalize_mode(value: str, name: str) -> str:
    """Normalize a security mode value and reject ambiguous configuration."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized_value = value.strip().lower()
    if normalized_value not in _SECURITY_MODE_VALUES:
        allowed_values = ", ".join(sorted(_SECURITY_MODE_VALUES))
        raise RuntimeError(f"{name} must be one of: {allowed_values}")
    return normalized_value


class Settings(BaseSettings):
    """Application settings loaded from .env."""

    app_name: str = "Yinshi"
    debug: bool = False

    # Database (legacy single-DB mode)
    db_path: str = "yinshi.db"

    # Multi-tenant databases
    control_db_path: str = "/var/lib/yinshi/control.db"
    user_data_dir: str = "/var/lib/yinshi/users"

    # Legacy pepper for wrapping per-user DEKs (hex string, 32+ bytes).
    # New deployments should use KEY_ENCRYPTION_KEY so wrapped DEKs carry a key id.
    encryption_pepper: str = ""
    key_encryption_key: str = ""
    key_encryption_key_id: str = "local-v1"
    key_encryption_keys_previous: SecretStr | None = None

    # Middle-ground data protection controls. "auto" enables the control in
    # authenticated non-debug deployments while keeping local tests explicit.
    tenant_db_encryption: str = "auto"
    control_field_encryption: str = "auto"
    user_data_encryption: str = "disabled"

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/callback/google"

    # GitHub OAuth
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/auth/callback/github"
    github_app_id: str = ""
    github_app_private_key_path: str = ""
    github_app_slug: str = ""
    github_app_client_id: str = ""
    github_app_client_secret: SecretStr | None = None
    github_app_user_callback_url: str = ""

    # Session secret for cookies -- generated randomly if not set
    secret_key: str = ""

    # Explicit flag to disable auth (empty google_client_id alone is not enough)
    disable_auth: bool = False

    # Sidecar
    sidecar_socket_path: str = "/tmp/yinshi-sidecar.sock"

    # Pi package update and release-note metadata
    pi_package_name: str = "@earendil-works/pi-coding-agent"
    pi_release_repository: str = "earendil-works/pi"
    # Encrypted database backups
    backup_dir: str = "/var/lib/yinshi/backups"
    backup_encryption_key: SecretStr | None = None

    # CORS
    frontend_url: str = "http://localhost:5173"

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # Production transport controls. "auto" requires HTTPS in authenticated
    # non-debug deployments and trusts the edge proxy to provide TLS.
    require_https: str = "auto"
    hsts_enabled: bool = True
    trusted_proxy_ips: str = "127.0.0.1,::1"
    trusted_hosts: str = "localhost,127.0.0.1,[::1]"
    request_body_max_bytes: int = 10 * 1024 * 1024

    # Allowed base directory for local repo imports (empty = reject all local imports)
    allowed_repo_base: str = ""

    # Per-user container isolation
    container_enabled: bool = True
    container_image: str = "yinshi-sidecar:latest"
    container_idle_timeout_s: int = 300
    container_memory_limit: str = "256m"
    container_cpu_quota: int = 50000
    container_pids_limit: int = 256
    container_max_count: int = 10
    container_socket_base: str = "/var/run/yinshi"
    container_mount_mode: str = "narrow"

    # Browser terminal runtime controls
    terminal_keepalive_s: int = 7200
    terminal_scrollback_lines: int = 1000

    # Thread hierarchy limits (docs/thread-orchestration.md)
    thread_hierarchy_enabled: bool = True
    agent_delegation_enabled: bool = False
    thread_max_depth: int = 1
    thread_max_direct_children: int = 4
    thread_max_active_descendants: int = 4
    thread_max_total: int = 20
    thread_max_spawns_per_turn: int = 4
    thread_wait_timeout_seconds_max: int = 60

    @field_validator(
        "thread_max_depth",
        "thread_max_direct_children",
        "thread_max_active_descendants",
        "thread_max_total",
        "thread_max_spawns_per_turn",
        "thread_wait_timeout_seconds_max",
    )
    @classmethod
    def _validate_thread_limit_positive(cls, value: int, info: object) -> int:
        """Reject nonpositive thread limits before any tree math runs."""
        field_name = getattr(info, "field_name", "thread limit")
        if value < 1:
            raise ValueError(f"thread {field_name} must be a positive integer")
        return value

    @model_validator(mode="after")
    def _validate_thread_limit_coherence(self) -> "Settings":
        """Keep thread limits inside coherent hard bounds."""
        hard_depth_limit = 32
        if self.thread_max_depth > hard_depth_limit:
            raise ValueError(f"thread thread_max_depth must not exceed {hard_depth_limit}")
        minimum_total = max(
            self.thread_max_direct_children,
            self.thread_max_active_descendants,
        )
        if self.thread_max_total <= minimum_total:
            raise ValueError(
                "thread thread_max_total must allow the direct children and "
                "active descendants limits"
            )
        return self

    # Managed Fly Sprites runtime
    managed_runtime_provider: str = "disabled"
    sprites_public_launch_enabled: bool = False
    sprites_storage_encryption_confirmed: bool = False
    sprites_api_token: SecretStr | None = None
    sprites_api_url: str = "https://api.sprites.dev/v1"
    sprites_name_prefix: str = "yinshi"
    sprites_name_key: SecretStr | None = None
    sprites_artifact_url: str = ""
    sprites_artifact_sha256: str = ""
    sprites_allowed_domains: str = ""
    sprites_public_control_url: str = ""
    sprites_bootstrap_script_path: str = ""
    sprites_wake_timeout_seconds: int = 30
    sprites_operation_stale_seconds: int = 1800
    sprites_reconcile_interval_seconds: int = 900
    sprites_reconcile_grace_seconds: int = 3600

    # Staging-only destructive recovery control
    deployment_environment: str = "local"
    managed_recovery_drill_enabled: bool = False
    managed_recovery_operator_token_hash: str = ""

    # Independent encrypted managed guest backups
    managed_backup_provider: str = "aws_s3"
    managed_backup_bucket: str = ""
    managed_backup_endpoint_url: str = ""
    managed_backup_region: str = ""
    managed_backup_access_key_id: SecretStr | None = None
    managed_backup_secret_access_key: SecretStr | None = None
    managed_backup_prefix: str = "yinshi-managed-v1"
    managed_backup_part_bytes: int = 16 * 1024 * 1024
    managed_backup_retention_days: int = 30

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        # The shared dotenv also contains sidecar provider credentials. Ignoring
        # unknown names prevents Pydantic from echoing their values on startup.
        "extra": "ignore",
    }

    @property
    def encryption_pepper_bytes(self) -> bytes:
        """Return the legacy encryption pepper as bytes."""
        return _decode_hex_secret(self.encryption_pepper, "ENCRYPTION_PEPPER")

    @property
    def key_encryption_key_bytes(self) -> bytes:
        """Return the current server-managed KEK bytes."""
        return _decode_hex_secret(self.key_encryption_key, "KEY_ENCRYPTION_KEY")

    @property
    def key_encryption_keyring_previous(self) -> dict[str, bytes]:
        """Return explicitly configured previous KEKs keyed by their stable IDs."""
        if self.key_encryption_keys_previous is None:
            return {}
        raw_keyring = self.key_encryption_keys_previous.get_secret_value().strip()
        if not raw_keyring:
            return {}
        try:
            payload = json.loads(raw_keyring)
        except json.JSONDecodeError as exc:
            raise RuntimeError("KEY_ENCRYPTION_KEYS_PREVIOUS must be a JSON object") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("KEY_ENCRYPTION_KEYS_PREVIOUS must be a JSON object")
        keyring: dict[str, bytes] = {}
        for raw_key_id, raw_key in payload.items():
            if not isinstance(raw_key_id, str) or not raw_key_id.strip():
                raise RuntimeError("Previous key IDs must be non-empty strings")
            if not isinstance(raw_key, str):
                raise RuntimeError("Previous KEK values must be hexadecimal strings")
            key_id = raw_key_id.strip()
            if key_id == self.key_encryption_key_id.strip():
                raise RuntimeError("Previous KEK IDs must differ from KEY_ENCRYPTION_KEY_ID")
            keyring[key_id] = _decode_hex_secret(raw_key, "previous KEK")
        return keyring

    @property
    def active_key_encryption_key_bytes(self) -> bytes:
        """Return the strongest configured key source for envelope encryption."""
        key_encryption_key_bytes = self.key_encryption_key_bytes
        if key_encryption_key_bytes:
            return key_encryption_key_bytes
        return self.encryption_pepper_bytes

    @property
    def tenant_db_encryption_mode(self) -> str:
        """Return the normalized tenant database encryption mode."""
        return _normalize_mode(self.tenant_db_encryption, "TENANT_DB_ENCRYPTION")

    @property
    def control_field_encryption_mode(self) -> str:
        """Return the normalized control-plane field encryption mode."""
        return _normalize_mode(self.control_field_encryption, "CONTROL_FIELD_ENCRYPTION")

    @property
    def user_data_encryption_mode(self) -> str:
        """Return the normalized filesystem encryption enforcement mode."""
        return _normalize_mode(self.user_data_encryption, "USER_DATA_ENCRYPTION")

    @property
    def require_https_mode(self) -> str:
        """Return the normalized HTTPS enforcement mode."""
        return _normalize_mode(self.require_https, "REQUIRE_HTTPS")

    @property
    def trusted_host_list(self) -> list[str]:
        """Return explicit hosts plus the configured frontend hostname."""
        configured_hosts = [
            host.strip().lower() for host in self.trusted_hosts.split(",") if host.strip()
        ]
        frontend_host = urlsplit(self.frontend_url).hostname
        if frontend_host:
            configured_hosts.append(frontend_host.lower())
        return list(dict.fromkeys(configured_hosts))

    @property
    def trusted_proxy_ip_set(self) -> set[str]:
        """Return normalized proxy addresses trusted to set forwarding headers."""
        return {
            address.strip().lower()
            for address in self.trusted_proxy_ips.split(",")
            if address.strip()
        }


def auth_is_enabled(settings: Settings) -> bool:
    """Return whether the operator explicitly enabled authentication."""
    if not isinstance(settings, Settings):
        raise TypeError("settings must be a Settings instance")
    return not settings.disable_auth


def _auth_is_enabled(settings: Settings) -> bool:
    """Backward-compatible wrapper for older internal tests and scripts."""
    return auth_is_enabled(settings)


def _mode_enabled(settings: Settings, mode: str) -> bool:
    """Resolve auto/enabled/required security modes against runtime posture."""
    if mode == "disabled":
        return False
    if mode == "enabled":
        return True
    if mode == "required":
        return True
    assert mode == "auto", "mode must be normalized before resolution"
    return auth_is_enabled(settings) and not settings.debug


def tenant_db_encryption_required(settings: Settings) -> bool:
    """Return whether tenant SQLite databases must use SQLCipher."""
    mode = settings.tenant_db_encryption_mode
    if mode == "enabled":
        return False
    return _mode_enabled(settings, mode)


def tenant_db_encryption_enabled(settings: Settings) -> bool:
    """Return whether tenant SQLite databases should use SQLCipher when possible."""
    return _mode_enabled(settings, settings.tenant_db_encryption_mode)


def control_field_encryption_enabled(settings: Settings) -> bool:
    """Return whether sensitive control-plane fields should be encrypted."""
    return _mode_enabled(settings, settings.control_field_encryption_mode)


def user_data_encryption_required(settings: Settings) -> bool:
    """Return whether user data directories must live on encrypted storage."""
    return _mode_enabled(settings, settings.user_data_encryption_mode)


def https_required(settings: Settings) -> bool:
    """Return whether HTTP requests must be upgraded or rejected in production."""
    mode = settings.require_https_mode
    if mode == "enabled":
        return True
    return _mode_enabled(settings, mode)


def _validate_settings(settings: Settings) -> None:
    """Reject invalid security-critical configuration."""
    if settings.sprites_public_launch_enabled and not settings.sprites_storage_encryption_confirmed:
        raise RuntimeError(
            "SPRITES_PUBLIC_LAUNCH_ENABLED requires " "SPRITES_STORAGE_ENCRYPTION_CONFIRMED=true"
        )
    if (
        settings.sprites_public_launch_enabled
        and settings.managed_runtime_provider != "fly_sprites"
    ):
        raise RuntimeError(
            "SPRITES_PUBLIC_LAUNCH_ENABLED requires MANAGED_RUNTIME_PROVIDER=fly_sprites"
        )
    if settings.managed_runtime_provider not in _MANAGED_RUNTIME_PROVIDER_VALUES:
        allowed_values = ", ".join(sorted(_MANAGED_RUNTIME_PROVIDER_VALUES))
        raise RuntimeError(f"MANAGED_RUNTIME_PROVIDER must be one of: {allowed_values}")
    if settings.managed_backup_provider not in _MANAGED_BACKUP_PROVIDER_VALUES:
        allowed_values = ", ".join(sorted(_MANAGED_BACKUP_PROVIDER_VALUES))
        raise RuntimeError(f"MANAGED_BACKUP_PROVIDER must be one of: {allowed_values}")
    if settings.deployment_environment not in {"local", "staging", "production"}:
        raise RuntimeError("DEPLOYMENT_ENVIRONMENT must be one of: local, production, staging")
    if settings.managed_recovery_drill_enabled:
        if settings.deployment_environment != "staging":
            raise RuntimeError(
                "MANAGED_RECOVERY_DRILL_ENABLED requires DEPLOYMENT_ENVIRONMENT=staging"
            )
        if _SHA256_PATTERN.fullmatch(settings.managed_recovery_operator_token_hash) is None:
            raise RuntimeError(
                "MANAGED_RECOVERY_OPERATOR_TOKEN_HASH must be a lowercase SHA-256 digest"
            )
    if settings.managed_backup_provider == "digitalocean_spaces":
        expected_endpoint = f"https://{settings.managed_backup_region}.digitaloceanspaces.com"
        if settings.managed_backup_endpoint_url != expected_endpoint:
            raise RuntimeError(
                "MANAGED_BACKUP_ENDPOINT_URL must be the DigitalOcean Spaces regional endpoint"
            )
    if settings.managed_runtime_provider == "fly_sprites":
        _require_https_url(
            settings.sprites_api_url,
            "SPRITES_API_URL",
            reject_routing_metadata=True,
        )
        _require_https_url(settings.sprites_artifact_url, "SPRITES_ARTIFACT_URL")
        _require_https_url(
            settings.sprites_public_control_url,
            "SPRITES_PUBLIC_CONTROL_URL",
            reject_routing_metadata=True,
        )
        _validate_allowed_domains(settings.sprites_allowed_domains)
        if _SPRITE_PREFIX_PATTERN.fullmatch(settings.sprites_name_prefix) is None:
            raise RuntimeError(
                "SPRITES_NAME_PREFIX must be a lowercase DNS label of 1 to 30 characters"
            )
        if _SHA256_PATTERN.fullmatch(settings.sprites_artifact_sha256) is None:
            raise RuntimeError(
                "SPRITES_ARTIFACT_SHA256 must be 64 lowercase hexadecimal characters"
            )
        if not 5 <= settings.sprites_wake_timeout_seconds <= 120:
            raise RuntimeError("SPRITES_WAKE_TIMEOUT_SECONDS must be between 5 and 120")
        if not 600 <= settings.sprites_operation_stale_seconds <= 86400:
            raise RuntimeError("SPRITES_OPERATION_STALE_SECONDS must be between 600 and 86400")
        if not 60 <= settings.sprites_reconcile_interval_seconds <= 86400:
            raise RuntimeError("SPRITES_RECONCILE_INTERVAL_SECONDS must be between 60 and 86400")
        if not 300 <= settings.sprites_reconcile_grace_seconds <= 604800:
            raise RuntimeError("SPRITES_RECONCILE_GRACE_SECONDS must be between 300 and 604800")
        if (
            not isinstance(settings.sprites_api_token, SecretStr)
            or not settings.sprites_api_token.get_secret_value().strip()
        ):
            raise RuntimeError("SPRITES_API_TOKEN is required for Fly Sprites")
        if not isinstance(settings.sprites_name_key, SecretStr):
            raise RuntimeError("SPRITES_NAME_KEY is required for Fly Sprites")
        sprites_name_key = settings.sprites_name_key.get_secret_value()
        if not sprites_name_key.strip():
            raise RuntimeError("SPRITES_NAME_KEY is required for Fly Sprites")
        if not settings.sprites_artifact_url:
            raise RuntimeError("SPRITES_ARTIFACT_URL is required for Fly Sprites")
        if not settings.sprites_artifact_sha256:
            raise RuntimeError("SPRITES_ARTIFACT_SHA256 is required for Fly Sprites")
        if not settings.sprites_public_control_url:
            raise RuntimeError("SPRITES_PUBLIC_CONTROL_URL is required for Fly Sprites")
        if settings.container_enabled:
            raise RuntimeError("Fly Sprites mode requires CONTAINER_ENABLED=false")
        if not auth_is_enabled(settings):
            raise RuntimeError("Fly Sprites mode requires AUTH_ENABLED=true")
        if not https_required(settings):
            raise RuntimeError("Fly Sprites mode requires HTTPS enforcement through REQUIRE_HTTPS")
        if settings.control_field_encryption_mode != "required":
            raise RuntimeError("Fly Sprites mode requires CONTROL_FIELD_ENCRYPTION=required")
        if not settings.managed_backup_bucket:
            raise RuntimeError("MANAGED_BACKUP_BUCKET is required for Fly Sprites")
        _require_https_url(
            settings.managed_backup_endpoint_url,
            "MANAGED_BACKUP_ENDPOINT_URL",
            reject_routing_metadata=True,
        )
        if not settings.managed_backup_region.strip():
            raise RuntimeError("MANAGED_BACKUP_REGION is required for Fly Sprites")
        if (
            not settings.managed_backup_prefix
            or len(settings.managed_backup_prefix) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-_/"
                for character in settings.managed_backup_prefix
            )
            or ".." in settings.managed_backup_prefix.split("/")
        ):
            raise RuntimeError("MANAGED_BACKUP_PREFIX is invalid")
        if not 5 * 1024 * 1024 <= settings.managed_backup_part_bytes <= 5 * 1024**3:
            raise RuntimeError("MANAGED_BACKUP_PART_BYTES is outside S3 multipart limits")
        if not 1 <= settings.managed_backup_retention_days <= 3650:
            raise RuntimeError("MANAGED_BACKUP_RETENTION_DAYS must be between 1 and 3650")
        access_key = settings.managed_backup_access_key_id
        secret_key = settings.managed_backup_secret_access_key
        if (access_key is None) != (secret_key is None):
            raise RuntimeError("MANAGED_BACKUP credentials must be configured together")
        if isinstance(access_key, SecretStr) and not access_key.get_secret_value().strip():
            raise RuntimeError("MANAGED_BACKUP credentials must not be blank")
        if isinstance(secret_key, SecretStr) and not secret_key.get_secret_value().strip():
            raise RuntimeError("MANAGED_BACKUP credentials must not be blank")
        if not isinstance(settings.backup_encryption_key, SecretStr):
            raise RuntimeError("BACKUP_ENCRYPTION_KEY is required for Fly Sprites")
        backup_encryption_key = settings.backup_encryption_key.get_secret_value()
        if _SHA256_PATTERN.fullmatch(backup_encryption_key) is None:
            raise RuntimeError("BACKUP_ENCRYPTION_KEY must be 64 lowercase hexadecimal characters")
        control_hostname = urlsplit(settings.sprites_public_control_url).hostname
        if not control_hostname or not _control_domain_is_allowed(
            control_hostname.lower(), settings.sprites_allowed_domains
        ):
            raise RuntimeError("SPRITES_ALLOWED_DOMAINS must cover SPRITES_PUBLIC_CONTROL_URL")
        if len(sprites_name_key.encode("utf-8")) < 32:
            raise RuntimeError("SPRITES_NAME_KEY must contain at least 32 UTF-8 bytes")
        bootstrap_script_path = Path(settings.sprites_bootstrap_script_path)
        if (
            not settings.sprites_bootstrap_script_path.strip()
            or not bootstrap_script_path.is_absolute()
            or not bootstrap_script_path.is_file()
        ):
            raise RuntimeError(
                "SPRITES_BOOTSTRAP_SCRIPT_PATH must be an absolute regular-file path"
            )

    authentication_enabled = auth_is_enabled(settings)
    if not authentication_enabled:
        normalized_host = settings.host.strip().lower()
        if normalized_host not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("No-auth mode must bind to a loopback host")
        if settings.container_enabled:
            raise RuntimeError("No-auth mode requires CONTAINER_ENABLED=false")
    if authentication_enabled:
        google_configured = bool(settings.google_client_id and settings.google_client_secret)
        github_configured = bool(settings.github_client_id and settings.github_client_secret)
        if not google_configured and not github_configured:
            raise RuntimeError(
                "At least one complete OAuth provider configuration is required when "
                "authentication is enabled"
            )
    if authentication_enabled and not settings.secret_key:
        raise RuntimeError("SECRET_KEY must be set when authentication is enabled")
    if authentication_enabled and len(settings.secret_key.encode("utf-8")) < 32:
        raise RuntimeError("SECRET_KEY must contain at least 32 bytes")
    if authentication_enabled and len(set(settings.secret_key)) < 8:
        raise RuntimeError("SECRET_KEY must contain at least 8 distinct characters")

    settings.encryption_pepper_bytes
    settings.key_encryption_key_bytes
    settings.key_encryption_keyring_previous

    if authentication_enabled:
        if not settings.debug:
            if not settings.active_key_encryption_key_bytes:
                raise RuntimeError(
                    "KEY_ENCRYPTION_KEY or ENCRYPTION_PEPPER must be set when "
                    "authentication is enabled outside debug mode"
                )

    required_key_material_modes: list[str] = []
    if tenant_db_encryption_required(settings):
        required_key_material_modes.append("TENANT_DB_ENCRYPTION")
    if settings.control_field_encryption_mode == "required":
        required_key_material_modes.append("CONTROL_FIELD_ENCRYPTION")
    if required_key_material_modes and not settings.active_key_encryption_key_bytes:
        raise RuntimeError(
            "KEY_ENCRYPTION_KEY or ENCRYPTION_PEPPER must be set when "
            f"{' or '.join(required_key_material_modes)} is required"
        )

    settings.tenant_db_encryption_mode
    settings.control_field_encryption_mode
    settings.user_data_encryption_mode
    settings.require_https_mode

    normalized_key_id = settings.key_encryption_key_id.strip()
    if settings.key_encryption_key_bytes and not normalized_key_id:
        raise RuntimeError("KEY_ENCRYPTION_KEY_ID must not be empty when KEY_ENCRYPTION_KEY is set")
    settings.key_encryption_key_id = normalized_key_id or "local-v1"

    if settings.container_mount_mode not in {"narrow", "tenant-data"}:
        raise RuntimeError("CONTAINER_MOUNT_MODE must be either narrow or tenant-data")
    if settings.terminal_keepalive_s < 300:
        raise RuntimeError("TERMINAL_KEEPALIVE_S must be at least 300 seconds")
    if settings.terminal_scrollback_lines < 100:
        raise RuntimeError("TERMINAL_SCROLLBACK_LINES must be at least 100")
    if settings.request_body_max_bytes < 1024:
        raise RuntimeError("REQUEST_BODY_MAX_BYTES must be at least 1024")
    if not settings.trusted_host_list:
        raise RuntimeError("TRUSTED_HOSTS must configure at least one host")
    if "*" in settings.trusted_host_list:
        raise RuntimeError("TRUSTED_HOSTS must not trust every host")
    settings.trusted_proxy_ip_set


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    settings = Settings()
    _validate_settings(settings)
    if not settings.secret_key:
        settings.secret_key = _generate_secret()
    return settings
