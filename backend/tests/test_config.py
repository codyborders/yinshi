"""Tests for application configuration."""

import pytest
from pydantic import SecretStr


def test_default_settings():
    """Settings should have sensible defaults."""
    from yinshi.config import Settings

    settings = Settings()
    assert settings.app_name == "Yinshi"
    assert settings.debug is False
    assert settings.db_path == "yinshi.db"
    assert settings.container_enabled is True
    assert settings.port == 8000


def test_managed_runtime_settings_default_to_disabled():
    """Managed runtime settings should preserve local and BYOC defaults."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
    )
    _validate_settings(settings)

    expected = {
        "managed_runtime_provider": "disabled",
        "sprites_public_launch_enabled": False,
        "sprites_api_token": None,
        "sprites_api_url": "https://api.sprites.dev/v1",
        "sprites_name_prefix": "yinshi",
        "sprites_name_key": None,
        "sprites_artifact_url": "",
        "sprites_artifact_sha256": "",
        "sprites_allowed_domains": "",
        "sprites_public_control_url": "",
        "sprites_bootstrap_script_path": "",
        "sprites_wake_timeout_seconds": 30,
        "sprites_operation_stale_seconds": 1800,
    }
    assert settings.model_dump(include=set(expected)) == expected


def test_fly_sprites_requires_host_containers_disabled():
    """Fly mode should reject the host container runtime."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=True,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        secret_key="test-session-secret-0123456789abcdef",
        key_encryption_key="b" * 64,
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="test-name-key",
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_public_control_url="https://control.example.com",
    )

    with pytest.raises(RuntimeError, match="CONTAINER_ENABLED=false"):
        _validate_settings(settings)


def test_fly_sprites_requires_authentication_enabled():
    """Fly mode should reject anonymous access."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="test-name-key",
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_public_control_url="https://control.example.com",
    )

    with pytest.raises(RuntimeError, match="AUTH_ENABLED=true"):
        _validate_settings(settings)


def test_managed_runtime_provider_rejects_unknown_value():
    """Managed runtime selection should reject unsupported providers."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="other",
    )

    with pytest.raises(RuntimeError, match="MANAGED_RUNTIME_PROVIDER"):
        _validate_settings(settings)


@pytest.mark.parametrize(
    ("field", "value", "error_name"),
    [
        ("sprites_api_token", None, "SPRITES_API_TOKEN"),
        ("sprites_api_token", "", "SPRITES_API_TOKEN"),
        ("sprites_name_key", None, "SPRITES_NAME_KEY"),
        ("sprites_name_key", "", "SPRITES_NAME_KEY"),
        ("sprites_artifact_url", "", "SPRITES_ARTIFACT_URL"),
        ("sprites_artifact_sha256", "", "SPRITES_ARTIFACT_SHA256"),
        ("sprites_public_control_url", "", "SPRITES_PUBLIC_CONTROL_URL"),
    ],
)
def test_fly_sprites_requires_managed_runtime_values(field, value, error_name):
    """Fly Sprites mode should fail closed when required values are absent."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        sprites_api_token="token",
        sprites_name_key="name-key",
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_public_control_url="https://control.example.com",
    )
    setattr(settings, field, value)

    with pytest.raises(RuntimeError, match=error_name):
        _validate_settings(settings)


def test_fly_sprites_requires_https_api_url():
    """Managed provider API should require a complete HTTPS URL."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_api_url="http://api.sprites.dev/v1",
        sprites_name_key="test-name-key",
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_public_control_url="https://control.example.com",
    )

    with pytest.raises(RuntimeError, match="SPRITES_API_URL"):
        _validate_settings(settings)


def test_fly_sprites_rejects_global_domain_wildcard():
    """Managed egress should reject the provider-wide wildcard."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="test-name-key",
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="*",
        sprites_public_control_url="https://control.example.com",
    )

    with pytest.raises(RuntimeError, match="SPRITES_ALLOWED_DOMAINS"):
        _validate_settings(settings)


@pytest.mark.parametrize(
    "domains",
    [
        "127.0.0.1",
        "[::1]",
        "https://example.com",
        "example.com:443",
        " example.com",
        "example.com ",
        "*example.com",
        "foo.*.example.com",
        "*.example.com*",
        "example.com,example.com",
        "Example.com",
        "localhost",
        "-bad.example",
        "bad-.example",
        "bad..example",
        ",example.com",
    ],
)
def test_fly_sprites_rejects_invalid_domain_entries(domains):
    """Managed egress entries should use strict lowercase public DNS patterns."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="test-name-key",
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains=domains,
        sprites_public_control_url="https://control.example.com",
    )

    with pytest.raises(RuntimeError, match="SPRITES_ALLOWED_DOMAINS"):
        _validate_settings(settings)


@pytest.mark.parametrize(
    ("field", "value", "error_name"),
    [
        ("sprites_artifact_url", "http://artifacts.example.com/a", "SPRITES_ARTIFACT_URL"),
        ("sprites_public_control_url", "http://control.example.com", "SPRITES_PUBLIC_CONTROL_URL"),
        ("sprites_api_url", "https://api.sprites.dev/v1?", "SPRITES_API_URL"),
        ("sprites_api_url", "https://api.sprites.dev/v1?token=x", "SPRITES_API_URL"),
        ("sprites_api_url", "https://api.sprites.dev/v1#", "SPRITES_API_URL"),
        ("sprites_api_url", "https://api.sprites.dev/v1#part", "SPRITES_API_URL"),
        ("sprites_api_url", "https://user@api.sprites.dev/v1", "SPRITES_API_URL"),
        ("sprites_api_url", "https://api.sprites.dev:8443/v1", "SPRITES_API_URL"),
        (
            "sprites_public_control_url",
            "https://control.example.com?token=x",
            "SPRITES_PUBLIC_CONTROL_URL",
        ),
        (
            "sprites_public_control_url",
            "https://control.example.com#part",
            "SPRITES_PUBLIC_CONTROL_URL",
        ),
        (
            "sprites_public_control_url",
            "https://user@control.example.com",
            "SPRITES_PUBLIC_CONTROL_URL",
        ),
        (
            "sprites_public_control_url",
            "https://control.example.com:8443",
            "SPRITES_PUBLIC_CONTROL_URL",
        ),
        ("sprites_artifact_sha256", "A" * 64, "SPRITES_ARTIFACT_SHA256"),
        ("sprites_artifact_sha256", "a" * 63, "SPRITES_ARTIFACT_SHA256"),
        ("sprites_artifact_sha256", "g" * 64, "SPRITES_ARTIFACT_SHA256"),
        ("sprites_name_prefix", "Yinshi", "SPRITES_NAME_PREFIX"),
        ("sprites_name_prefix", "-yinshi", "SPRITES_NAME_PREFIX"),
        ("sprites_name_prefix", "yinshi-", "SPRITES_NAME_PREFIX"),
        ("sprites_name_prefix", "a" * 31, "SPRITES_NAME_PREFIX"),
        ("sprites_wake_timeout_seconds", 4, "SPRITES_WAKE_TIMEOUT_SECONDS"),
        ("sprites_wake_timeout_seconds", 121, "SPRITES_WAKE_TIMEOUT_SECONDS"),
        ("sprites_operation_stale_seconds", 599, "SPRITES_OPERATION_STALE_SECONDS"),
        ("sprites_operation_stale_seconds", 86401, "SPRITES_OPERATION_STALE_SECONDS"),
    ],
)
def test_fly_sprites_rejects_invalid_managed_values(field, value, error_name):
    """Managed values should satisfy URL, digest, prefix, and timeout constraints."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="test-name-key",
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_public_control_url="https://control.example.com",
    )
    setattr(settings, field, value)

    with pytest.raises(RuntimeError, match=error_name):
        _validate_settings(settings)


def test_fly_sprites_public_launch_cannot_be_enabled():
    """Public Fly launch should remain blocked until both requirements exist."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="fly_sprites",
        sprites_public_launch_enabled=True,
    )

    with pytest.raises(RuntimeError) as error_info:
        _validate_settings(settings)

    assert str(error_info.value) == (
        "SPRITES_PUBLIC_LAUNCH_ENABLED cannot be true until off-provider managed guest "
        "backup/restore and trustworthy Sprite storage-encryption verification are implemented"
    )


def test_fly_sprites_accepts_valid_bounded_settings():
    """Fly mode should accept valid wildcard domains and timeout boundaries."""
    from pathlib import Path

    from pydantic import SecretStr

    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=False,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        secret_key="test-session-secret-0123456789abcdef",
        key_encryption_key="b" * 64,
        backup_encryption_key="c" * 64,
        control_field_encryption="required",
        require_https="required",
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_api_url="https://api.sprites.dev:443/v1",
        sprites_name_key="é" * 16,
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="api.github.com,*.example.com",
        sprites_public_control_url="https://control.example.com:443",
        sprites_bootstrap_script_path=str(Path(__file__).resolve()),
        sprites_wake_timeout_seconds=5,
        sprites_operation_stale_seconds=86400,
    )

    _validate_settings(settings)
    assert isinstance(settings.sprites_api_token, SecretStr)
    assert isinstance(settings.sprites_name_key, SecretStr)
    assert "test-token" not in repr(settings)
    assert "é" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value", "error_name"),
    [
        ("require_https", "disabled", "REQUIRE_HTTPS"),
        ("control_field_encryption", "enabled", "CONTROL_FIELD_ENCRYPTION"),
        ("backup_encryption_key", None, "BACKUP_ENCRYPTION_KEY"),
        ("backup_encryption_key", "short", "BACKUP_ENCRYPTION_KEY"),
        ("backup_encryption_key", "z" * 64, "BACKUP_ENCRYPTION_KEY"),
        ("sprites_name_key", "é" * 15, "SPRITES_NAME_KEY"),
        ("sprites_allowed_domains", "registry.npmjs.org", "SPRITES_ALLOWED_DOMAINS"),
        ("sprites_bootstrap_script_path", "", "SPRITES_BOOTSTRAP_SCRIPT_PATH"),
        ("sprites_bootstrap_script_path", "relative.sh", "SPRITES_BOOTSTRAP_SCRIPT_PATH"),
        (
            "sprites_bootstrap_script_path",
            "/missing/bootstrap.sh",
            "SPRITES_BOOTSTRAP_SCRIPT_PATH",
        ),
    ],
)
def test_fly_sprites_rejects_incomplete_security_configuration(field, value, error_name):
    """Fly mode should reject missing managed security requirements."""
    from pathlib import Path

    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=False,
        container_enabled=False,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        secret_key="test-session-secret-0123456789abcdef",
        key_encryption_key="b" * 64,
        backup_encryption_key="c" * 64,
        control_field_encryption="required",
        require_https="required",
        managed_runtime_provider="fly_sprites",
        sprites_api_token="test-token",
        sprites_name_key="n" * 32,
        sprites_artifact_url="https://artifacts.example.com/runner.pyz",
        sprites_artifact_sha256="a" * 64,
        sprites_allowed_domains="registry.npmjs.org,control.example.com",
        sprites_public_control_url="https://control.example.com",
        sprites_bootstrap_script_path=str(Path(__file__).resolve()),
    )
    if field in {"backup_encryption_key", "sprites_name_key"} and isinstance(value, str):
        setattr(settings, field, SecretStr(value))
    else:
        setattr(settings, field, value)

    with pytest.raises(RuntimeError, match=error_name):
        _validate_settings(settings)


def test_disabled_managed_provider_ignores_unused_sprites_values():
    """Disabled managed mode should not change local or BYOC validation."""
    from yinshi.config import Settings, _validate_settings

    settings = Settings(
        _env_file=None,
        disable_auth=True,
        container_enabled=False,
        managed_runtime_provider="disabled",
        sprites_api_url="http://invalid",
        sprites_name_prefix="INVALID",
        sprites_artifact_url="http://invalid",
        sprites_artifact_sha256="invalid",
        sprites_allowed_domains="*",
        sprites_public_control_url="http://invalid",
        sprites_wake_timeout_seconds=0,
        sprites_operation_stale_seconds=0,
    )

    _validate_settings(settings)


def test_settings_ignores_unknown_dotenv_secrets(tmp_path):
    """Sidecar-only dotenv keys should not leak through settings validation errors."""
    from yinshi.config import Settings

    dotenv_path = tmp_path / ".env"
    secret_marker = "synthetic-provider-secret-marker"
    dotenv_path.write_text(f"UNRECOGNIZED_PROVIDER_KEY={secret_marker}\n", encoding="utf-8")

    settings = Settings(_env_file=dotenv_path)

    assert settings.app_name == "Yinshi"
    assert secret_marker not in repr(settings)


def test_no_auth_mode_rejects_non_loopback_bind(monkeypatch):
    """Anonymous development mode must not listen on a remote interface."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("HOST", "0.0.0.0")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="loopback"):
        get_settings()
    get_settings.cache_clear()


def test_no_auth_mode_rejects_container_posture(monkeypatch):
    """Anonymous mode must use the explicit local host-side development posture."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("CONTAINER_ENABLED", "true")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="CONTAINER_ENABLED=false"):
        get_settings()
    get_settings.cache_clear()


def test_settings_from_env(monkeypatch):
    """Settings should read from environment variables."""
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DB_PATH", "/tmp/test.db")
    monkeypatch.setenv("PORT", "9000")

    from yinshi.config import Settings

    settings = Settings()
    assert settings.debug is True
    assert settings.db_path == "/tmp/test.db"
    assert settings.port == 9000


def test_get_settings_cached(monkeypatch):
    """get_settings should return the same instance."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()


def test_authenticated_mode_requires_an_oauth_provider(monkeypatch):
    """Explicit authenticated mode should fail closed without an OAuth provider."""
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="OAuth provider"):
        get_settings()
    get_settings.cache_clear()


def test_auth_enabled_requires_explicit_secret_key(monkeypatch):
    """Auth-enabled settings should fail fast without an explicit secret key."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.delenv("SECRET_KEY", raising=False)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        get_settings()
    get_settings.cache_clear()


def test_authenticated_mode_rejects_short_session_secret(monkeypatch):
    """Authenticated deployments should require a 32-character session secret."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "s" * 31)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="at least 32"):
        get_settings()
    get_settings.cache_clear()


def test_authenticated_mode_rejects_low_diversity_session_secret(monkeypatch):
    """A long repeated character should not qualify as a session secret."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "s" * 64)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="distinct characters"):
        get_settings()
    get_settings.cache_clear()


def test_short_encryption_pepper_is_rejected(monkeypatch):
    """ENCRYPTION_PEPPER should fail fast when it is shorter than 32 bytes."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("ENCRYPTION_PEPPER", "aa")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        get_settings()
    get_settings.cache_clear()


def test_key_encryption_key_requires_key_id(monkeypatch):
    """Server-managed KEKs should carry a non-empty key id for rotation."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("KEY_ENCRYPTION_KEY_ID", "   ")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="KEY_ENCRYPTION_KEY_ID"):
        get_settings()
    get_settings.cache_clear()


def test_invalid_security_mode_is_rejected(monkeypatch):
    """Security mode environment values should fail fast when misspelled."""
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("CONTAINER_ENABLED", "false")
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "sometimes")

    from yinshi.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="TENANT_DB_ENCRYPTION"):
        get_settings()
    get_settings.cache_clear()


def test_auto_tenant_db_encryption_is_required_in_authenticated_production(monkeypatch):
    """Auto mode should fail closed for tenant DB encryption in production auth mode."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-secret")
    monkeypatch.setenv("DISABLE_AUTH", "false")
    monkeypatch.setenv("SECRET_KEY", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("KEY_ENCRYPTION_KEY", "b" * 64)
    monkeypatch.setenv("TENANT_DB_ENCRYPTION", "auto")
    monkeypatch.setenv("DEBUG", "false")

    from yinshi.config import get_settings, tenant_db_encryption_required

    get_settings.cache_clear()
    settings = get_settings()
    assert tenant_db_encryption_required(settings) is True
    get_settings.cache_clear()
