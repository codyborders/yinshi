"""Property-based tests for invariant-heavy backend helpers."""

from __future__ import annotations

import string

import pytest
from hypothesis import assume, given, strategies as st

from yinshi.exceptions import GitError


def _tamper(payload: bytes) -> bytes:
    """Flip one byte so authenticated decrypt operations should fail."""
    last_byte = payload[-1] ^ 0x01
    return payload[:-1] + bytes([last_byte])


@given(st.text())
def test_summarize_prompt_properties(prompt: str) -> None:
    """Prompt summaries stay short, lowercased, and whitespace-free."""
    from yinshi.api.stream import _summarize_prompt

    result = _summarize_prompt(prompt)
    assert len(result) <= 50
    assert result == result.lower()
    assert not any(character.isspace() for character in result)


@given(
    dek=st.binary(min_size=32, max_size=32),
    user_id=st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=32),
    pepper=st.binary(min_size=32, max_size=64),
)
def test_wrap_unwrap_roundtrip_property(
    dek: bytes,
    user_id: str,
    pepper: bytes,
) -> None:
    """Wrapped DEKs should round-trip for the same user and pepper."""
    from yinshi.services.crypto import unwrap_dek, wrap_dek

    wrapped = wrap_dek(dek, user_id, pepper)
    assert unwrap_dek(wrapped, user_id, pepper) == dek


@given(
    dek=st.binary(min_size=32, max_size=32),
    user_id=st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=32),
    wrong_user_id=st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=32),
    pepper=st.binary(min_size=32, max_size=64),
)
def test_unwrap_dek_wrong_user_or_tampering_fails_property(
    dek: bytes,
    user_id: str,
    wrong_user_id: str,
    pepper: bytes,
) -> None:
    """Wrong users and tampered payloads should not unwrap successfully."""
    from yinshi.services.crypto import unwrap_dek, wrap_dek

    assume(user_id != wrong_user_id)

    wrapped = wrap_dek(dek, user_id, pepper)

    with pytest.raises(Exception):
        unwrap_dek(wrapped, wrong_user_id, pepper)

    with pytest.raises(Exception):
        unwrap_dek(_tamper(wrapped), user_id, pepper)


@given(
    dek=st.binary(min_size=32, max_size=32),
    user_id=st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=32),
    pepper=st.binary(min_size=32, max_size=64),
    wrong_pepper=st.binary(min_size=32, max_size=64),
)
def test_unwrap_dek_wrong_pepper_fails_property(
    dek: bytes,
    user_id: str,
    pepper: bytes,
    wrong_pepper: bytes,
) -> None:
    """Wrong peppers should not unwrap successfully."""
    from yinshi.services.crypto import unwrap_dek, wrap_dek

    assume(pepper != wrong_pepper)
    wrapped = wrap_dek(dek, user_id, pepper)

    with pytest.raises(Exception):
        unwrap_dek(wrapped, user_id, wrong_pepper)


@given(
    api_key=st.text(
        alphabet=string.ascii_letters + string.digits + "-_",
        min_size=16,
        max_size=64,
    ),
    dek=st.binary(min_size=32, max_size=32),
)
def test_encrypt_decrypt_api_key_roundtrip_property(
    api_key: str,
    dek: bytes,
) -> None:
    """Encrypted API keys should round-trip without leaking plaintext bytes."""
    from yinshi.services.crypto import decrypt_api_key, encrypt_api_key

    encrypted = encrypt_api_key(api_key, dek)
    assert decrypt_api_key(encrypted, dek) == api_key
    assert encrypted != api_key.encode()
    assert api_key.encode() not in encrypted


@given(
    api_key=st.text(
        alphabet=string.ascii_letters + string.digits + "-_",
        min_size=16,
        max_size=64,
    ),
    dek=st.binary(min_size=32, max_size=32),
    wrong_dek=st.binary(min_size=32, max_size=32),
)
def test_decrypt_api_key_wrong_dek_or_tampering_fails_property(
    api_key: str,
    dek: bytes,
    wrong_dek: bytes,
) -> None:
    """Wrong DEKs and tampered payloads should fail decryption."""
    from yinshi.services.crypto import decrypt_api_key, encrypt_api_key

    assume(dek != wrong_dek)
    encrypted = encrypt_api_key(api_key, dek)

    with pytest.raises(Exception):
        decrypt_api_key(encrypted, wrong_dek)

    with pytest.raises(Exception):
        decrypt_api_key(_tamper(encrypted), dek)


@given(
    username=st.one_of(
        st.none(),
        st.text(
            alphabet=string.ascii_lowercase + string.digits + "-_",
            min_size=1,
            max_size=16,
        ),
    ),
)
def test_generate_branch_name_property(username: str | None) -> None:
    """Generated branch names should keep the adjective-noun-suffix shape."""
    from yinshi.services.git import generate_branch_name

    branch = generate_branch_name(username=username)
    bare = branch.split("/", 1)[1] if username else branch
    parts = bare.split("-")

    if username:
        assert branch.startswith(f"{username}/")

    assert len(parts) == 3
    assert all(parts[:2])
    assert len(parts[2]) == 4
    assert all(character in string.ascii_lowercase + string.digits for character in parts[2])


@given(
    prefix=st.sampled_from(["-", "ext::", "file://"]),
    tail=st.text(
        alphabet=string.ascii_letters + string.digits + "/:._-@",
        min_size=0,
        max_size=64,
    ),
)
def test_validate_clone_url_rejects_dangerous_prefixes_property(
    prefix: str,
    tail: str,
) -> None:
    """Dangerous clone prefixes should always be rejected."""
    from yinshi.services.git import _validate_clone_url

    with pytest.raises(GitError):
        _validate_clone_url(f"{prefix}{tail}")


@given(
    prefix=st.sampled_from(["https://", "ssh://", "git@"]),
    tail=st.text(
        alphabet=string.ascii_letters + string.digits + "/:._-@",
        min_size=1,
        max_size=64,
    ),
)
def test_validate_clone_url_accepts_allowed_prefixes_property(
    prefix: str,
    tail: str,
) -> None:
    """Allowed clone prefixes should pass validation."""
    from yinshi.services.git import _validate_clone_url

    _validate_clone_url(f"{prefix}{tail}")


@given(
    input_tokens=st.integers(min_value=0, max_value=10**9),
    output_tokens=st.integers(min_value=0, max_value=10**9),
    cache_read_tokens=st.integers(min_value=0, max_value=10**9),
    cache_write_tokens=st.integers(min_value=0, max_value=10**9),
)
def test_estimate_cost_cents_non_negative_property(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> None:
    """Cost estimates should stay non-negative for valid usage payloads."""
    from yinshi.services.keys import estimate_cost_cents

    cost = estimate_cost_cents(
        "minimax",
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
        },
    )
    assert cost >= 0


@given(
    provider=st.sampled_from(["anthropic", "openai", "bedrock"]),
    input_tokens=st.integers(min_value=0, max_value=10**9),
    output_tokens=st.integers(min_value=0, max_value=10**9),
)
def test_estimate_cost_cents_non_minimax_zero_property(
    provider: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Non-MiniMax providers should continue to estimate as zero."""
    from yinshi.services.keys import estimate_cost_cents

    cost = estimate_cost_cents(
        provider,
        {"input_tokens": input_tokens, "output_tokens": output_tokens},
    )
    assert cost == 0.0
