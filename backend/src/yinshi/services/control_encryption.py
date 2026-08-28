"""Field-level encryption helpers for sensitive control-plane values."""

from __future__ import annotations

from cryptography.exceptions import InvalidTag

from yinshi.config import control_field_encryption_enabled, get_settings
from yinshi.exceptions import EncryptionNotConfiguredError
from yinshi.services.crypto import decrypt_text, derive_subkey, encrypt_text, is_encrypted_text


def _control_field_keys() -> tuple[bytes, ...]:
    """Derive current and previous AES keys for control-field rotation overlap."""
    settings = get_settings()
    master_key = settings.active_key_encryption_key_bytes
    if not master_key:
        raise EncryptionNotConfiguredError(
            "KEY_ENCRYPTION_KEY or ENCRYPTION_PEPPER is required for control-field encryption"
        )
    keys = [derive_subkey(master_key, purpose="control-field", context="v1")]
    for previous_key in settings.key_encryption_keyring_previous.values():
        derived_key = derive_subkey(previous_key, purpose="control-field", context="v1")
        if derived_key not in keys:
            keys.append(derived_key)
    return tuple(keys)


def _control_field_key() -> bytes:
    """Return the current AES key used for new control-field encryption."""
    return _control_field_keys()[0]


def _aad(field_name: str, user_id: str) -> str:
    """Build stable AAD so encrypted control fields cannot be copied between users."""
    if not isinstance(field_name, str):
        raise TypeError("field_name must be a string")
    normalized_field_name = field_name.strip()
    if not normalized_field_name:
        raise ValueError("field_name must not be empty")
    if not isinstance(user_id, str):
        raise TypeError("user_id must be a string")
    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise ValueError("user_id must not be empty")
    return f"{normalized_field_name}:{normalized_user_id}"


def encrypt_control_text(field_name: str, user_id: str, plaintext: str | None) -> str | None:
    """Encrypt a sensitive control-plane text field when policy requires it."""
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a string or None")
    settings = get_settings()
    if not control_field_encryption_enabled(settings):
        return plaintext
    # The input is always new plaintext. A string that merely carries the
    # envelope prefix is not trusted as an existing envelope, so it is
    # encrypted like any other value and cannot bypass envelope creation.
    return encrypt_text(plaintext, _control_field_key(), aad=_aad(field_name, user_id))


def decrypt_control_text(field_name: str, user_id: str, stored_value: str | None) -> str | None:
    """Decrypt an encrypted control-plane text field and pass through plaintext legacy values."""
    if stored_value is None:
        return None
    if not isinstance(stored_value, str):
        raise TypeError("stored_value must be a string or None")
    if not is_encrypted_text(stored_value):
        return stored_value
    associated_data = _aad(field_name, user_id)
    for key in _control_field_keys():
        try:
            return decrypt_text(stored_value, key, aad=associated_data)
        except InvalidTag:
            continue
        except ValueError:
            # The payload does not parse as an envelope this code could have
            # written, so treat it as legacy plaintext rather than failing the
            # read. Genuine envelopes are always parseable.
            return stored_value
    raise InvalidTag("No configured control-field key could decrypt the value")


def control_field_is_stored_envelope(
    field_name: str,
    user_id: str,
    stored_value: str | None,
) -> bool:
    """Return whether a stored value decrypts as a trusted control-field envelope.

    A string prefix alone is not trust. Only a payload that decrypts under a
    configured key counts, so legacy plaintext that happens to carry the
    envelope prefix is treated as plaintext and remains eligible for
    encryption.
    """
    if not isinstance(stored_value, str) or not is_encrypted_text(stored_value):
        return False
    associated_data = _aad(field_name, user_id)
    for key in _control_field_keys():
        try:
            decrypt_text(stored_value, key, aad=associated_data)
            return True
        except InvalidTag:
            continue
        except ValueError:
            return False
    return False
