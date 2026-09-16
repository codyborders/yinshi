"""Durable session attachment storage outside Git worktrees."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

ATTACHMENT_BYTES_MAX = 50 * 1024 * 1024
ATTACHMENT_COUNT_MAX = 8
ATTACHMENT_PROMPT_BYTES_MAX = 50 * 1024 * 1024
ATTACHMENT_CHUNK_BYTES = 24_000
IMAGE_MEDIA_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True, slots=True)
class SessionAttachment:
    """Validated metadata for one session attachment."""

    id: str
    session_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256_hex: str
    status: str
    next_chunk_index: int
    received_bytes: int
    turn_id: str | None
    path: str

    @property
    def is_image(self) -> bool:
        """Return whether Pi can receive this file as an image block."""
        return self.media_type in IMAGE_MEDIA_TYPES


def _validate_resource_id(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _validate_filename(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("attachment filename must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > 255
        or len(value.encode("utf-8")) > 255
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value in {".", ".."}
    ):
        raise ValueError("attachment filename must be a simple filename")
    return value


def _validate_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("attachment SHA-256 is invalid")
    return value


def _validate_size(value: int) -> int:
    if type(value) is not int or not 1 <= value <= ATTACHMENT_BYTES_MAX:
        raise ValueError("attachment size must be between 1 byte and 50MB")
    return value


def _attachment_directory(data_dir: str, session_id: str) -> Path:
    _validate_resource_id(session_id, "session ID")
    if not isinstance(data_dir, str) or not data_dir.strip():
        raise ValueError("attachment data directory is invalid")
    configured_root = Path(data_dir)
    if configured_root.is_symlink():
        raise ValueError("attachment data directory is invalid")
    configured_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_root = configured_root.resolve()
    if not data_root.is_dir():
        raise ValueError("attachment data directory is invalid")
    current = data_root
    for part in ("attachments", "sessions", session_id):
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
            raise ValueError("attachment directory is invalid")
        os.chmod(current, 0o700)
    return current


def attachment_path(data_dir: str, session_id: str, attachment_id: str) -> Path:
    """Return the final path for one validated attachment identity."""
    _validate_resource_id(attachment_id, "attachment ID")
    return _attachment_directory(data_dir, session_id) / attachment_id


def _partial_path(data_dir: str, session_id: str, attachment_id: str) -> Path:
    return attachment_path(data_dir, session_id, attachment_id).with_suffix(".part")


def _row_to_attachment(row: sqlite3.Row, data_dir: str) -> SessionAttachment:
    attachment_id = _validate_resource_id(str(row["id"]), "attachment ID")
    session_id = _validate_resource_id(str(row["session_id"]), "session ID")
    return SessionAttachment(
        id=attachment_id,
        session_id=session_id,
        filename=_validate_filename(str(row["filename"])),
        media_type=str(row["media_type"]),
        size_bytes=_validate_size(int(row["size_bytes"])),
        sha256_hex=_validate_digest(str(row["sha256"])),
        status=str(row["status"]),
        next_chunk_index=int(row["next_chunk_index"]),
        received_bytes=int(row["received_bytes"]),
        turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
        path=str(attachment_path(data_dir, session_id, attachment_id)),
    )


def _reap_expired_uploads(database: sqlite3.Connection, data_dir: str, session_id: str) -> None:
    """Remove incomplete uploads that have been idle for fifteen minutes."""
    rows = database.execute(
        """SELECT id FROM attachments
           WHERE session_id = ? AND status = 'uploading'
             AND updated_at <= datetime('now', '-15 minutes')""",
        (session_id,),
    ).fetchall()
    for row in rows:
        attachment_id = str(row["id"])
        _partial_path(data_dir, session_id, attachment_id).unlink(missing_ok=True)
        attachment_path(data_dir, session_id, attachment_id).unlink(missing_ok=True)
    if rows:
        database.executemany(
            "DELETE FROM attachments WHERE id = ? AND session_id = ?",
            ((str(row["id"]), session_id) for row in rows),
        )
        database.commit()


def start_attachment(
    database: sqlite3.Connection,
    *,
    data_dir: str,
    session_id: str,
    filename: str,
    size_bytes: int,
    sha256_hex: str,
) -> SessionAttachment:
    """Reserve one bounded attachment and create its private partial file."""
    _validate_resource_id(session_id, "session ID")
    filename = _validate_filename(filename)
    size_bytes = _validate_size(size_bytes)
    sha256_hex = _validate_digest(sha256_hex)
    if database.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
        raise LookupError("session not found")
    _reap_expired_uploads(database, data_dir, session_id)
    pending = database.execute(
        """SELECT count(*) AS attachment_count, COALESCE(sum(size_bytes), 0) AS attachment_bytes
           FROM attachments WHERE session_id = ? AND status IN ('uploading', 'ready')
             AND turn_id IS NULL""",
        (session_id,),
    ).fetchone()
    assert pending is not None, "attachment quota query must return one row"
    if int(pending["attachment_count"]) >= ATTACHMENT_COUNT_MAX:
        raise ValueError("attachment count limit reached")
    if int(pending["attachment_bytes"]) + size_bytes > ATTACHMENT_PROMPT_BYTES_MAX:
        raise ValueError("attachment byte limit reached")
    attachment_id = uuid.uuid4().hex
    partial_path = _partial_path(data_dir, session_id, attachment_id)
    descriptor = os.open(
        partial_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    try:
        database.execute(
            """INSERT INTO attachments
               (id, session_id, filename, media_type, size_bytes, sha256, status,
                next_chunk_index, received_bytes)
               VALUES (?, ?, ?, 'application/octet-stream', ?, ?, 'uploading', 0, 0)""",
            (attachment_id, session_id, filename, size_bytes, sha256_hex),
        )
        database.commit()
    except BaseException:
        partial_path.unlink(missing_ok=True)
        raise
    row = database.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row is not None, "created attachment must be queryable"
    return _row_to_attachment(row, data_dir)


def _open_regular_file(path: Path, flags: int) -> int:
    """Open one private regular attachment without following a link."""
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("attachment file is invalid")
    return descriptor


def append_attachment_chunk(
    database: sqlite3.Connection,
    *,
    data_dir: str,
    session_id: str,
    attachment_id: str,
    chunk_index: int,
    chunk: bytes,
) -> SessionAttachment:
    """Append one fixed-size chunk or accept an exact retry."""
    _validate_resource_id(session_id, "session ID")
    _validate_resource_id(attachment_id, "attachment ID")
    if type(chunk_index) is not int or chunk_index < 0:
        raise ValueError("attachment chunk index is invalid")
    if not isinstance(chunk, bytes) or not chunk or len(chunk) > ATTACHMENT_CHUNK_BYTES:
        raise ValueError("attachment chunk length is invalid")
    database.execute("BEGIN IMMEDIATE")
    try:
        row = database.execute(
            "SELECT * FROM attachments WHERE id = ? AND session_id = ?", (attachment_id, session_id)
        ).fetchone()
        if row is None:
            raise LookupError("attachment not found")
        attachment = _row_to_attachment(row, data_dir)
        if attachment.status != "uploading":
            raise ValueError("attachment is not accepting chunks")
        offset = chunk_index * ATTACHMENT_CHUNK_BYTES
        expected_length = min(ATTACHMENT_CHUNK_BYTES, attachment.size_bytes - offset)
        if expected_length <= 0 or len(chunk) != expected_length:
            raise ValueError("attachment chunk length does not match its position")
        partial_path = _partial_path(data_dir, session_id, attachment_id)
        if chunk_index < attachment.next_chunk_index:
            descriptor = _open_regular_file(partial_path, os.O_RDONLY)
            with os.fdopen(descriptor, "rb") as stream:
                stream.seek(offset)
                stored_chunk = stream.read(expected_length)
            if stored_chunk != chunk:
                raise ValueError("attachment retry does not match stored chunk")
            database.rollback()
            return attachment
        if chunk_index != attachment.next_chunk_index:
            raise ValueError("attachment chunk sequence is not contiguous")
        if attachment.received_bytes != offset:
            raise RuntimeError("attachment byte offset is inconsistent")
        descriptor = _open_regular_file(partial_path, os.O_RDWR)
        with os.fdopen(descriptor, "r+b", buffering=0) as stream:
            stream.seek(offset)
            written = stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if written != len(chunk):
            raise OSError("attachment chunk write was incomplete")
        database.execute(
            """UPDATE attachments SET next_chunk_index = next_chunk_index + 1,
                   received_bytes = received_bytes + ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND session_id = ?""",
            (len(chunk), attachment_id, session_id),
        )
        database.commit()
    except BaseException:
        database.rollback()
        raise
    updated = database.execute(
        "SELECT * FROM attachments WHERE id = ? AND session_id = ?", (attachment_id, session_id)
    ).fetchone()
    assert updated is not None, "updated attachment must be queryable"
    return _row_to_attachment(updated, data_dir)


def _detect_media_type(path: Path) -> str:
    with path.open("rb") as stream:
        prefix = stream.read(8192)
    if prefix.startswith(bytes.fromhex("89504e470d0a1a0a")):
        return "image/png"
    if prefix.startswith(bytes.fromhex("ffd8ff")):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    if bytes((0,)) not in prefix:
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return "text/plain"
    return "application/octet-stream"


def complete_attachment(
    database: sqlite3.Connection, *, data_dir: str, session_id: str, attachment_id: str
) -> SessionAttachment:
    """Verify one complete upload and atomically publish its final file."""
    _validate_resource_id(session_id, "session ID")
    _validate_resource_id(attachment_id, "attachment ID")
    database.execute("BEGIN IMMEDIATE")
    try:
        row = database.execute(
            "SELECT * FROM attachments WHERE id = ? AND session_id = ?", (attachment_id, session_id)
        ).fetchone()
        if row is None:
            raise LookupError("attachment not found")
        attachment = _row_to_attachment(row, data_dir)
        if attachment.status == "ready":
            database.rollback()
            return attachment
        if attachment.status != "uploading":
            raise ValueError("attachment cannot be completed")
        partial_path = _partial_path(data_dir, session_id, attachment_id)
        file_stat = partial_path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise ValueError("attachment partial file is invalid")
        if file_stat.st_size != attachment.size_bytes:
            raise ValueError("attachment size does not match declaration")
        digest = hashlib.sha256()
        with partial_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != attachment.sha256_hex:
            raise ValueError("attachment digest does not match declaration")
        media_type = _detect_media_type(partial_path)
        final_path = attachment_path(data_dir, session_id, attachment_id)
        os.replace(partial_path, final_path)
        os.chmod(final_path, 0o600)
        database.execute(
            """UPDATE attachments SET status = 'ready', media_type = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ? AND session_id = ?""",
            (media_type, attachment_id, session_id),
        )
        database.commit()
    except BaseException:
        database.rollback()
        raise
    updated = database.execute(
        "SELECT * FROM attachments WHERE id = ? AND session_id = ?", (attachment_id, session_id)
    ).fetchone()
    assert updated is not None, "completed attachment must be queryable"
    return _row_to_attachment(updated, data_dir)


def delete_attachment(
    database: sqlite3.Connection, *, data_dir: str, session_id: str, attachment_id: str
) -> None:
    """Delete one unclaimed attachment and its partial or final file."""
    _validate_resource_id(session_id, "session ID")
    _validate_resource_id(attachment_id, "attachment ID")
    row = database.execute(
        "SELECT * FROM attachments WHERE id = ? AND session_id = ?", (attachment_id, session_id)
    ).fetchone()
    if row is None:
        return
    attachment = _row_to_attachment(row, data_dir)
    if attachment.turn_id is not None:
        raise ValueError("attachment is already bound to a prompt")
    attachment_path(data_dir, session_id, attachment_id).unlink(missing_ok=True)
    _partial_path(data_dir, session_id, attachment_id).unlink(missing_ok=True)
    database.execute(
        "DELETE FROM attachments WHERE id = ? AND session_id = ?", (attachment_id, session_id)
    )
    database.commit()


def claim_prompt_attachments(
    database: sqlite3.Connection,
    *,
    data_dir: str,
    session_id: str,
    attachment_ids: list[str],
    turn_id: str,
) -> list[SessionAttachment]:
    """Bind one exact ready attachment set to a prompt turn."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session ID is invalid")
    if attachment_ids:
        _validate_resource_id(turn_id, "turn ID")
    elif not isinstance(turn_id, str) or not turn_id:
        raise ValueError("turn ID is invalid")
    if len(attachment_ids) > ATTACHMENT_COUNT_MAX or len(set(attachment_ids)) != len(
        attachment_ids
    ):
        raise ValueError("prompt attachment IDs are invalid")
    attachments: list[SessionAttachment] = []
    for attachment_id in attachment_ids:
        _validate_resource_id(attachment_id, "attachment ID")
        row = database.execute(
            "SELECT * FROM attachments WHERE id = ? AND session_id = ?", (attachment_id, session_id)
        ).fetchone()
        if row is None:
            raise LookupError("attachment not found")
        attachment = _row_to_attachment(row, data_dir)
        if attachment.status != "ready":
            raise ValueError("attachment upload is incomplete")
        if attachment.turn_id not in {None, turn_id}:
            raise ValueError("attachment is already bound to another prompt")
        file_stat = Path(attachment.path).lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_size != attachment.size_bytes
        ):
            raise ValueError("attachment file is unavailable")
        attachments.append(attachment)
    if sum(item.size_bytes for item in attachments) > ATTACHMENT_PROMPT_BYTES_MAX:
        raise ValueError("prompt attachment byte limit reached")
    existing_ids = {
        str(row["id"])
        for row in database.execute(
            "SELECT id FROM attachments WHERE session_id = ? AND turn_id = ?", (session_id, turn_id)
        ).fetchall()
    }
    if existing_ids and existing_ids != set(attachment_ids):
        raise ValueError("prompt attachment set does not match its retry")
    if attachment_ids:
        placeholders = ",".join("?" for _ in attachment_ids)
        database.execute(
            f"""UPDATE attachments SET turn_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ? AND id IN ({placeholders})""",  # noqa: S608
            (turn_id, session_id, *attachment_ids),
        )
    return attachments


def attachment_display_content(prompt: str, attachments: list[SessionAttachment]) -> str:
    """Add stable attachment labels to the stored user transcript."""
    if not attachments:
        return prompt
    names = "\n".join(f"- {attachment.filename}" for attachment in attachments)
    return f"{prompt}\n\nAttachments:\n{names}"


def delete_session_attachment_files(
    data_dir: str, session_ids: list[str] | tuple[str, ...]
) -> None:
    """Remove durable attachment files for deleted sessions."""
    import shutil

    if not isinstance(data_dir, str) or not data_dir.strip():
        raise ValueError("attachment data directory is invalid")
    data_root = Path(data_dir).resolve()
    attachments_root = data_root / "attachments"
    sessions_root = attachments_root / "sessions"
    for directory in (attachments_root, sessions_root):
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise ValueError("attachment sessions directory is invalid")
    for session_id in session_ids:
        try:
            _validate_resource_id(session_id, "session ID")
        except ValueError:
            continue
        session_path = sessions_root / session_id
        if session_path.is_symlink():
            raise ValueError("attachment session directory is invalid")
        if session_path.exists():
            shutil.rmtree(session_path)
