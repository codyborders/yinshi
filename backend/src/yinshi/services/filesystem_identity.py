"""Linux descriptor-backed filesystem identity helpers."""

from __future__ import annotations

from pathlib import Path


def descriptor_mount_id(descriptor: int) -> int:
    """Return the Linux mount ID for one open descriptor."""
    if type(descriptor) is not int or descriptor < 0:
        raise RuntimeError("mount identity descriptor is invalid")
    try:
        raw = Path(f"/proc/self/fdinfo/{descriptor}").read_bytes()
    except OSError as error:
        raise RuntimeError("mount identity is unavailable") from error
    values = [line.split()[1] for line in raw.splitlines() if line.startswith(b"mnt_id:")]
    if len(values) != 1:
        raise RuntimeError("mount identity is invalid")
    try:
        value = int(values[0])
    except ValueError as error:
        raise RuntimeError("mount identity is invalid") from error
    if value < 0:
        raise RuntimeError("mount identity is invalid")
    return value
