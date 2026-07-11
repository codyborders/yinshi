"""Tests desktop helper readiness bytes against the Electron protocol contract."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.conftest import _configure_test_env
from yinshi.desktop_runtime import DESKTOP_HELPER_PROTOCOL_VERSION, serialize_ready_message


def test_serialize_ready_message_emits_strict_pipe_contract() -> None:
    """Readiness bytes should carry only validated protocol fields and one newline."""
    nonce = "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD"

    payload = serialize_ready_message(port=43123, instance_nonce=nonce)

    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert json.loads(payload) == {
        "type": "ready",
        "protocolVersion": DESKTOP_HELPER_PROTOCOL_VERSION,
        "port": 43123,
        "instanceNonce": nonce,
    }


@pytest.mark.parametrize(
    ("port", "nonce", "error"),
    [
        (0, "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD", "port"),
        (65536, "abcdefghijklmnopqrstuvwxyz_1234567890-ABCD", "port"),
        (43123, "short", "instance_nonce"),
        (43123, "contains spaces and remains far too short", "instance_nonce"),
    ],
)
def test_serialize_ready_message_rejects_invalid_values(
    port: int,
    nonce: str,
    error: str,
) -> None:
    """Invalid readiness fields must fail before reaching the inherited pipe."""
    with pytest.raises(ValueError, match=error):
        serialize_ready_message(port=port, instance_nonce=nonce)


def test_desktop_helper_serves_packaged_app_after_pipe_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Helper should bind loopback, signal readiness privately, and serve health."""
    _configure_test_env(monkeypatch, tmp_path, auth_enabled=False)
    asset_dir = tmp_path / "frontend"
    asset_dir.mkdir()
    (asset_dir / "index.html").write_text("<main>Packaged Yinshi</main>", encoding="utf-8")
    read_fd, write_fd = os.pipe()
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "yinshi.desktop_runtime",
            "--ready-fd",
            str(write_fd),
            "--asset-dir",
            str(asset_dir),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        pass_fds=(write_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    os.close(write_fd)

    ready_pipe = os.fdopen(read_fd, "rb", closefd=True)
    try:
        readable, _, _ = select.select([ready_pipe], [], [], 10)
        assert readable, "desktop helper did not signal readiness"
        ready_line = ready_pipe.readline()
        assert ready_line, "desktop helper closed readiness pipe without a message"
        ready = json.loads(ready_line)
        response = httpx.get(f"http://127.0.0.1:{ready['port']}/health", timeout=5.0)
        app_response = httpx.get(f"http://127.0.0.1:{ready['port']}/", timeout=5.0)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert app_response.text == "<main>Packaged Yinshi</main>"
        assert "default-src 'self'" in app_response.headers["Content-Security-Policy"]
    finally:
        ready_pipe.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()
