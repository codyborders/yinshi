"""Verify the hosted relay refuses a second process on one deployment host."""

import subprocess
import sys
from pathlib import Path

from yinshi.services.relay_process_lock import RelayProcessLock

_CHILD_SCRIPT = """
import sys
from pathlib import Path
from yinshi.services.relay_process_lock import RelayProcessLock

lock = RelayProcessLock(Path(sys.argv[1]))
try:
    lock.acquire()
except RuntimeError:
    raise SystemExit(42)
lock.release()
"""


def _child_result(lock_path: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(lock_path)],
        check=False,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode in {0, 42}
    assert not result.stdout
    return result


def test_relay_process_lock_is_shared_inside_one_process(tmp_path: Path) -> None:
    """Multiple hosted app objects in one process share one kernel lock."""
    lock_path = tmp_path / "relay.lock"
    first = RelayProcessLock(lock_path)
    second = RelayProcessLock(lock_path)

    first.acquire()
    second.acquire()
    second.release()
    assert _child_result(lock_path).returncode == 42
    first.release()

    assert _child_result(lock_path).returncode == 0
    assert lock_path.stat().st_mode & 0o777 == 0o600
