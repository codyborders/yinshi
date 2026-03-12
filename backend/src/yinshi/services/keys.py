"""BYOK key resolution and freemium usage tracking.

Handles API key lookup, platform credit enforcement, cost estimation,
and usage recording for the freemium model.
"""

import logging
import uuid

from yinshi.config import get_settings
from yinshi.db import get_control_db
from yinshi.exceptions import CreditExhaustedError, EncryptionNotConfiguredError, KeyNotFoundError
from yinshi.services.crypto import decrypt_api_key, generate_dek, unwrap_dek, wrap_dek

logger = logging.getLogger(__name__)

# MiniMax M2.5 Highspeed pricing (per 1M tokens, in cents)
_MINIMAX_COSTS = {
    "input": 30,         # $0.30/M
    "output": 120,       # $1.20/M
    "cache_read": 3,     # $0.03/M
    "cache_write": 3.75, # $0.0375/M
}


def get_user_dek(user_id: str) -> bytes:
    """Retrieve and unwrap the user's DEK from the control DB."""
    settings = get_settings()
    pepper = settings.encryption_pepper_bytes
    if not pepper:
        raise EncryptionNotConfiguredError("Encryption pepper not configured")

    with get_control_db() as db:
        row = db.execute(
            "SELECT encrypted_dek FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        raise KeyNotFoundError(f"User {user_id} not found")

    if not row["encrypted_dek"]:
        # Lazy-generate DEK for accounts created before encryption was configured
        dek = generate_dek()
        encrypted_dek = wrap_dek(dek, user_id, pepper)
        with get_control_db() as db:
            db.execute(
                "UPDATE users SET encrypted_dek = ? WHERE id = ?",
                (encrypted_dek, user_id),
            )
            db.commit()
        logger.info("Generated DEK for user %s (legacy account)", user_id)
        return dek

    return unwrap_dek(row["encrypted_dek"], user_id, pepper)


def resolve_user_api_key(user_id: str, provider: str) -> str | None:
    """Look up and decrypt the user's stored BYOK key for a provider.

    Returns the plaintext API key, or None if no key is stored.
    """
    with get_control_db() as db:
        row = db.execute(
            "SELECT encrypted_key FROM api_keys WHERE user_id = ? AND provider = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, provider),
        ).fetchone()

        if not row:
            return None

        dek = get_user_dek(user_id)
        key = decrypt_api_key(row["encrypted_key"], dek)

        db.execute(
            "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        db.commit()

    return key


def get_credit_remaining_cents(user_id: str) -> int:
    """Return remaining freemium credit in cents."""
    with get_control_db() as db:
        row = db.execute(
            "SELECT credit_limit_cents, credit_used_cents FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return 0

    return max(0, row["credit_limit_cents"] - row["credit_used_cents"])


def resolve_api_key_for_prompt(
    user_id: str, provider: str, platform_key: str | None
) -> tuple[str, str]:
    """Resolve which API key to use for a prompt.

    Returns (api_key, key_source) where key_source is 'byok' or 'platform'.

    Raises CreditExhaustedError or KeyNotFoundError if no key is available.
    """
    # 1. Check for BYOK key
    byok_key = resolve_user_api_key(user_id, provider)
    if byok_key:
        return byok_key, "byok"

    # 2. Platform key for minimax with remaining credit
    if provider == "minimax" and platform_key:
        remaining = get_credit_remaining_cents(user_id)
        if remaining > 0:
            return platform_key, "platform"

        raise CreditExhaustedError(
            "Free credit exhausted. Add your own MiniMax API key in Settings."
        )

    # 3. No key available
    raise KeyNotFoundError(
        f"No API key found for {provider}. Add one in Settings."
    )


def estimate_cost_cents(provider: str, usage: dict) -> float:
    """Estimate cost from token counts. Returns cents.

    Only MiniMax costs are tracked (platform credit). Other providers
    return 0 since the user pays directly via BYOK.
    """
    if provider != "minimax":
        return 0.0

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_tokens", 0)
    cache_write = usage.get("cache_write_tokens", 0)

    cost = (
        input_tokens * _MINIMAX_COSTS["input"]
        + output_tokens * _MINIMAX_COSTS["output"]
        + cache_read * _MINIMAX_COSTS["cache_read"]
        + cache_write * _MINIMAX_COSTS["cache_write"]
    ) / 1_000_000

    return cost


def record_usage(
    user_id: str,
    session_id: str,
    provider: str,
    model: str,
    usage: dict,
    key_source: str,
) -> None:
    """Insert a usage_log row. If key_source='platform', increment credit_used_cents."""
    cost = estimate_cost_cents(provider, usage)
    row_id = uuid.uuid4().hex

    with get_control_db() as db:
        db.execute(
            "INSERT INTO usage_log "
            "(id, user_id, session_id, provider, model, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "cost_cents, key_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                user_id,
                session_id,
                provider,
                model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_read_tokens", 0),
                usage.get("cache_write_tokens", 0),
                cost,
                key_source,
            ),
        )

        if key_source == "platform" and cost > 0:
            db.execute(
                "UPDATE users SET credit_used_cents = credit_used_cents + ? WHERE id = ?",
                (int(round(cost)), user_id),
            )

        db.commit()

    logger.info(
        "Usage recorded: user=%s provider=%s model=%s cost=%.2fc source=%s",
        user_id, provider, model, cost, key_source,
    )
