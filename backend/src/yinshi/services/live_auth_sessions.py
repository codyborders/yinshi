"""In-process revocation signals for long-lived authenticated connections."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiveAuthSessionRegistration:
    """One event-loop-bound connection waiting for auth-session revocation."""

    user_id: str
    auth_session_id: str
    event: asyncio.Event
    loop: asyncio.AbstractEventLoop

    def close(self) -> None:
        """Remove this registration after its connection closes."""
        _remove_registration(self)


_registry_lock = threading.Lock()
_registrations_by_session: dict[str, dict[int, LiveAuthSessionRegistration]] = {}
_registrations_by_user: dict[str, dict[int, LiveAuthSessionRegistration]] = {}


def _normalize_identifier(value: str, name: str) -> str:
    """Return one non-empty registry identifier."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"{name} must not be empty")
    return normalized_value


def register_live_auth_session(
    user_id: str,
    auth_session_id: str,
) -> LiveAuthSessionRegistration:
    """Register one long-lived connection on the current event loop."""
    normalized_user_id = _normalize_identifier(user_id, "user_id")
    normalized_auth_session_id = _normalize_identifier(auth_session_id, "auth_session_id")
    loop = asyncio.get_running_loop()
    registration = LiveAuthSessionRegistration(
        user_id=normalized_user_id,
        auth_session_id=normalized_auth_session_id,
        event=asyncio.Event(),
        loop=loop,
    )
    registration_key = id(registration)
    with _registry_lock:
        _registrations_by_session.setdefault(normalized_auth_session_id, {})[
            registration_key
        ] = registration
        _registrations_by_user.setdefault(normalized_user_id, {})[registration_key] = registration
    return registration


def register_live_desktop_device(
    user_id: str,
    device_id: str,
) -> LiveAuthSessionRegistration:
    """Register one connection bound to a desktop device."""
    normalized_device_id = _normalize_identifier(device_id, "device_id")
    return register_live_auth_session(
        user_id=user_id,
        auth_session_id=f"desktop-device:{normalized_device_id}",
    )


def _remove_registration(registration: LiveAuthSessionRegistration) -> None:
    """Remove one registration from both indexes without assuming it exists."""
    if not isinstance(registration, LiveAuthSessionRegistration):
        raise TypeError("registration must be a LiveAuthSessionRegistration")
    registration_key = id(registration)
    with _registry_lock:
        session_registrations = _registrations_by_session.get(registration.auth_session_id)
        if session_registrations is not None:
            session_registrations.pop(registration_key, None)
            if not session_registrations:
                _registrations_by_session.pop(registration.auth_session_id, None)
        user_registrations = _registrations_by_user.get(registration.user_id)
        if user_registrations is not None:
            user_registrations.pop(registration_key, None)
            if not user_registrations:
                _registrations_by_user.pop(registration.user_id, None)


def _signal_registrations(registrations: tuple[LiveAuthSessionRegistration, ...]) -> None:
    """Signal registrations safely from event-loop or worker threads."""
    for registration in registrations:
        try:
            registration.loop.call_soon_threadsafe(registration.event.set)
        except RuntimeError:
            _remove_registration(registration)


def signal_auth_session_revoked(auth_session_id: str) -> None:
    """Wake every local connection created by one auth session."""
    normalized_auth_session_id = _normalize_identifier(auth_session_id, "auth_session_id")
    with _registry_lock:
        registrations = tuple(
            _registrations_by_session.get(normalized_auth_session_id, {}).values()
        )
    _signal_registrations(registrations)


def signal_desktop_device_revoked(device_id: str) -> None:
    """Wake every local connection created by one desktop device."""
    normalized_device_id = _normalize_identifier(device_id, "device_id")
    signal_auth_session_revoked(f"desktop-device:{normalized_device_id}")


def signal_user_sessions_revoked(user_id: str) -> None:
    """Wake every local connection owned by one user."""
    normalized_user_id = _normalize_identifier(user_id, "user_id")
    with _registry_lock:
        registrations = tuple(_registrations_by_user.get(normalized_user_id, {}).values())
    _signal_registrations(registrations)
