"""Git operations: clone repos and manage worktrees."""

import asyncio
import logging
import os
import re
import secrets
import stat
import string
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from yinshi.exceptions import GitError

logger = logging.getLogger(__name__)

_ADJECTIVES = [
    "swift",
    "bold",
    "calm",
    "dark",
    "keen",
    "warm",
    "cool",
    "pure",
    "wise",
    "fast",
    "bright",
    "quiet",
    "sharp",
    "smooth",
    "steady",
    "gentle",
    "vivid",
    "grand",
    "noble",
    "fresh",
    "prime",
    "lunar",
    "solar",
    "amber",
    "coral",
    "ivory",
    "olive",
    "azure",
]
_NOUNS = [
    "fox",
    "owl",
    "elk",
    "wolf",
    "hawk",
    "bear",
    "lynx",
    "crane",
    "drake",
    "finch",
    "heron",
    "raven",
    "otter",
    "tiger",
    "eagle",
    "falcon",
    "panda",
    "bison",
    "cedar",
    "maple",
    "river",
    "stone",
    "flame",
    "frost",
    "storm",
    "ridge",
    "grove",
    "brook",
]

_GIT_COMMAND_TIMEOUT_S = 300.0
_GIT_EXECUTABLE_PATH = "/usr/bin/git"
_GITHUB_HOST = "github.com"


class _StableDirectory(str):
    """A named directory retained by descriptor for one external effect sequence."""

    descriptor: int
    device: int
    inode: int

    def __new__(cls, path: str, descriptor: int) -> "_StableDirectory":  # noqa: PYI034
        instance = super().__new__(cls, os.path.abspath(path))
        identity = os.fstat(descriptor)
        instance.descriptor = descriptor
        instance.device = identity.st_dev
        instance.inode = identity.st_ino
        return instance

    def process_path(self, *, modeled: bool) -> str:
        """Return a child-visible stable locator or a modeled-test fallback."""
        if sys.platform.startswith("linux"):
            return f"/proc/self/fd/{self.descriptor}"
        if modeled:
            return str(self)
        raise GitError("Stable Git directory descriptors are unavailable on this platform")


def _directory_open_flags() -> int:
    """Return no-follow directory flags shared by retained Git objects."""
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _stable_directory_name_matches(directory: _StableDirectory) -> bool:
    """Return whether a retained directory still owns its accepted name."""
    try:
        named = os.lstat(directory)
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(named.st_mode)
        and not stat.S_ISLNK(named.st_mode)
        and (named.st_dev, named.st_ino) == (directory.device, directory.inode)
    )


def _require_exclusive_directory(
    descriptor: int,
    *,
    expected_device: int | None = None,
) -> os.stat_result:
    """Require one broker-owned namespace inaccessible to other identities."""
    identity = os.fstat(descriptor)
    exclusive = (
        stat.S_ISDIR(identity.st_mode)
        and identity.st_uid == os.geteuid()
        and identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0
        and (expected_device is None or identity.st_dev == expected_device)
    )
    if not exclusive:
        raise GitError("Git directory lacks exclusive ownership")
    return identity


@contextmanager
def _retain_directory(path: str) -> Iterator[_StableDirectory]:
    """Retain one exact no-follow directory object until its effects finish."""
    absolute = os.path.abspath(path)
    try:
        descriptor = os.open(absolute, _directory_open_flags())
    except OSError as exc:
        raise GitError("Git working directory is unavailable") from exc
    directory = _StableDirectory(absolute, descriptor)
    try:
        if not _stable_directory_name_matches(directory):
            raise GitError("Git working directory changed during open")
        yield directory
    finally:
        os.close(descriptor)


@contextmanager
def _retain_directory_argument(path: str) -> Iterator[_StableDirectory]:
    """Reuse an already-retained argument or retain its current named object."""
    if isinstance(path, _StableDirectory):
        yield path
        return
    with _retain_directory(path) as directory:
        yield directory


def _require_stable_directory_name(
    directory: _StableDirectory,
    *,
    object_name: str,
) -> None:
    """Fail when an accepted directory no longer owns its original name."""
    if not _stable_directory_name_matches(directory):
        raise GitError(f"Git {object_name} changed during operation")


def _stable_directory_is_empty(directory: _StableDirectory) -> bool:
    """Return whether a retained directory has no entries."""
    with os.scandir(directory.descriptor) as entries:
        return next(entries, None) is None


@contextmanager
def _retain_parent_directory(path: Path) -> Iterator[_StableDirectory]:
    """Retain or create an exact parent through one exclusive descriptor chain."""
    absolute_parent = Path(os.path.abspath(path)).parent
    missing: list[str] = []
    existing = absolute_parent
    while not os.path.lexists(existing):
        missing.append(existing.name)
        if existing == existing.parent:
            raise GitError("Git destination parent is unavailable")
        existing = existing.parent

    with ExitStack() as stack:
        parent = stack.enter_context(_retain_directory(str(existing)))
        parent_identity = _require_exclusive_directory(parent.descriptor)
        current = existing
        for component in reversed(missing):
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent.descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(component, _directory_open_flags(), dir_fd=parent.descriptor)
            stack.callback(os.close, descriptor)
            child_identity = _require_exclusive_directory(
                descriptor,
                expected_device=parent_identity.st_dev,
            )
            named = os.stat(component, dir_fd=parent.descriptor, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (child_identity.st_dev, child_identity.st_ino):
                raise GitError("Git destination parent changed during creation")
            current /= component
            parent = _StableDirectory(str(current), descriptor)
            parent_identity = child_identity
        yield parent


@contextmanager
def _retain_new_directory(path: Path) -> Iterator[_StableDirectory]:
    """Create and retain one exact destination below a retained parent."""
    absolute = Path(os.path.abspath(path))
    with _retain_parent_directory(absolute) as parent:
        parent_identity = _require_exclusive_directory(parent.descriptor)
        try:
            os.mkdir(absolute.name, mode=0o700, dir_fd=parent.descriptor)
        except FileExistsError as exc:
            raise GitError("Destination already exists but is not a git repository") from exc
        descriptor = os.open(
            absolute.name,
            _directory_open_flags(),
            dir_fd=parent.descriptor,
        )
        directory = _StableDirectory(str(absolute), descriptor)
        try:
            opened = _require_exclusive_directory(
                descriptor,
                expected_device=parent_identity.st_dev,
            )
            named = os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                raise GitError("Git destination changed during creation")
            yield directory
        finally:
            os.close(descriptor)


@contextmanager
def _retain_empty_worktree(path: str) -> Iterator[_StableDirectory]:
    """Retain an existing empty worktree stage or create one securely."""
    if isinstance(path, _StableDirectory):
        _require_exclusive_directory(path.descriptor)
        if not _stable_directory_is_empty(path):
            raise GitError("Worktree path already exists but is not a git repository")
        yield path
        return
    absolute = Path(os.path.abspath(path))
    with _retain_parent_directory(absolute) as parent:
        parent_identity = _require_exclusive_directory(parent.descriptor)
        try:
            named = os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            named = None
        if named is None:
            try:
                os.mkdir(absolute.name, mode=0o700, dir_fd=parent.descriptor)
            except FileExistsError as exc:
                raise GitError("Worktree path changed during creation") from exc
        descriptor = os.open(absolute.name, _directory_open_flags(), dir_fd=parent.descriptor)
        directory = _StableDirectory(str(absolute), descriptor)
        try:
            opened = _require_exclusive_directory(
                descriptor,
                expected_device=parent_identity.st_dev,
            )
            current = os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise GitError("Git worktree target changed during open")
            if not _stable_directory_is_empty(directory):
                raise GitError("Worktree path already exists but is not a git repository")
            yield directory
        finally:
            os.close(descriptor)


def _read_descriptor_file(parent: int, name: str) -> bytes:
    """Read one no-follow Git metadata file through its retained parent."""
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            raise GitError("Git worktree metadata changed during cleanup")
        value = os.read(descriptor, 4097)
        if len(value) > 4096:
            raise GitError("Git worktree metadata is too large")
        return value
    finally:
        os.close(descriptor)


def _remove_directory_contents(descriptor: int) -> None:
    """Remove entries only below one retained exclusive metadata directory."""
    parent_identity = _require_exclusive_directory(descriptor)
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if before.st_dev != parent_identity.st_dev or before.st_uid != os.geteuid():
            raise GitError("Git worktree metadata lacks exclusive ownership")
        if stat.S_ISDIR(before.st_mode) and not stat.S_ISLNK(before.st_mode):
            child = os.open(name, _directory_open_flags(), dir_fd=descriptor)
            try:
                opened = _require_exclusive_directory(
                    child,
                    expected_device=parent_identity.st_dev,
                )
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise GitError("Git worktree metadata changed during cleanup")
                _remove_directory_contents(child)
                named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise GitError("Git worktree metadata changed during cleanup")
                os.rmdir(name, dir_fd=descriptor)
            finally:
                os.close(child)
        elif stat.S_ISREG(before.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(child)
                latest = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (
                    latest.st_dev,
                    latest.st_ino,
                ) != (opened.st_dev, opened.st_ino):
                    raise GitError("Git worktree metadata changed during cleanup")
                os.unlink(name, dir_fd=descriptor)
            finally:
                os.close(child)
        else:
            raise GitError("Git worktree metadata has an unsupported file type")
    os.fsync(descriptor)


def _remove_missing_worktree_registration(
    repository: _StableDirectory,
    worktree_path: str,
) -> None:
    """Remove only metadata whose backlink names one absent selected worktree."""
    repository_identity = _require_exclusive_directory(repository.descriptor)
    git_directory = os.open(".git", _directory_open_flags(), dir_fd=repository.descriptor)
    try:
        git_identity = _require_exclusive_directory(
            git_directory,
            expected_device=repository_identity.st_dev,
        )
        try:
            worktrees_directory = os.open(
                "worktrees", _directory_open_flags(), dir_fd=git_directory
            )
        except FileNotFoundError:
            return
        try:
            _require_exclusive_directory(
                worktrees_directory,
                expected_device=git_identity.st_dev,
            )
            expected = os.fsencode(f"{os.path.abspath(worktree_path)}/.git")
            matches: list[tuple[str, int, int]] = []
            with os.scandir(worktrees_directory) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                try:
                    candidate = os.open(name, _directory_open_flags(), dir_fd=worktrees_directory)
                except OSError:
                    continue
                try:
                    identity = _require_exclusive_directory(
                        candidate,
                        expected_device=git_identity.st_dev,
                    )
                    if _read_descriptor_file(candidate, "gitdir").strip() == expected:
                        matches.append((name, identity.st_dev, identity.st_ino))
                except (GitError, OSError):
                    pass
                finally:
                    os.close(candidate)
            if not matches:
                return
            if len(matches) != 1:
                raise GitError("Git worktree registration is unavailable")
            name, device, inode = matches[0]
            candidate = os.open(name, _directory_open_flags(), dir_fd=worktrees_directory)
            try:
                opened = os.fstat(candidate)
                if (opened.st_dev, opened.st_ino) != (device, inode):
                    raise GitError("Git worktree metadata changed during cleanup")
                _remove_directory_contents(candidate)
                named = os.stat(name, dir_fd=worktrees_directory, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (device, inode):
                    raise GitError("Git worktree metadata changed during cleanup")
                os.rmdir(name, dir_fd=worktrees_directory)
                os.fsync(worktrees_directory)
                os.fsync(git_directory)
            finally:
                os.close(candidate)
        finally:
            os.close(worktrees_directory)
    finally:
        os.close(git_directory)


async def _remove_worktree(
    repository: _StableDirectory,
    worktree_path: str,
) -> None:
    """Remove one exact existing worktree or one exact stale registration."""
    if isinstance(worktree_path, _StableDirectory):
        await _run_git(["worktree", "remove", "--force", worktree_path], cwd=repository)
        return
    absolute = Path(os.path.abspath(worktree_path))
    claimed_absent = not os.path.lexists(absolute)
    if not os.path.lexists(absolute.parent):
        _remove_missing_worktree_registration(repository, str(absolute))
        return
    try:
        with _retain_directory(str(absolute.parent)) as parent:
            parent_identity = _require_exclusive_directory(parent.descriptor)
            try:
                named = os.stat(
                    absolute.name,
                    dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                claimed_absent = True
            if claimed_absent:
                _remove_missing_worktree_registration(repository, str(absolute))
                return
            descriptor = os.open(
                absolute.name,
                _directory_open_flags(),
                dir_fd=parent.descriptor,
            )
            worktree = _StableDirectory(str(absolute), descriptor)
            try:
                opened = _require_exclusive_directory(
                    descriptor,
                    expected_device=parent_identity.st_dev,
                )
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise GitError("Git worktree path changed during cleanup")
                await _run_git(["worktree", "remove", "--force", worktree], cwd=repository)
            finally:
                os.close(descriptor)
    except GitError:
        if os.path.lexists(absolute.parent):
            raise
        _remove_missing_worktree_registration(repository, str(absolute))


def generate_branch_name(username: str | None = None) -> str:
    """Generate a random branch name like 'username/swift-fox-a3f2'."""
    adjective = secrets.choice(_ADJECTIVES)
    noun = secrets.choice(_NOUNS)
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    bare = f"{adjective}-{noun}-{suffix}"
    if username:
        return f"{username}/{bare}"
    return bare


def _validate_clone_url(url: str) -> None:
    """Allow only canonical GitHub HTTPS repository URLs on the host."""
    if not isinstance(url, str):
        raise TypeError("url must be a string")
    if url.startswith("-"):
        raise GitError("Invalid repository URL")
    if url.startswith(("ext::", "file://")):
        raise GitError("URL scheme not allowed")

    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitError("Only canonical GitHub HTTPS repository URLs are allowed") from exc
    path_parts = [part for part in parsed.path.split("/") if part]
    canonical = (
        parsed.scheme == "https"
        and parsed.hostname == _GITHUB_HOST
        and parsed.username is None
        and parsed.password is None
        and port is None
        and len(path_parts) == 2
        and not parsed.query
        and not parsed.fragment
    )
    if not canonical:
        raise GitError("Only canonical GitHub HTTPS repository URLs are allowed")


@contextmanager
def _git_askpass_env(access_token: str | None) -> Iterator[dict[str, str] | None]:
    """Provide temporary environment variables for HTTPS token auth."""
    if access_token is None:
        yield None
        return

    if not access_token:
        raise GitError("Git access token must not be empty")

    with tempfile.TemporaryDirectory(prefix="yinshi-git-askpass-") as temp_dir:
        askpass_path = Path(temp_dir) / "askpass.sh"
        askpass_path.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
            "  *) printf '%s\\n' \"$YINSHI_GIT_TOKEN\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass_path.chmod(0o700)
        yield {
            "GIT_ASKPASS": str(askpass_path),
            "GIT_TERMINAL_PROMPT": "0",
            "YINSHI_GIT_TOKEN": access_token,
        }


async def _terminate_and_drain_git_process(process: asyncio.subprocess.Process) -> None:
    """Kill and drain one piped Git child before returning."""
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    drain_task = asyncio.create_task(process.communicate())
    while not drain_task.done():
        try:
            await asyncio.shield(drain_task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001 - drain waits out the local effect
            break
    if not drain_task.cancelled():
        with suppress(BaseException):
            drain_task.result()


async def _read_bounded_git_stream(
    stream: asyncio.StreamReader | None,
    *,
    byte_limit: int | None,
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bool]:
    """Read one child stream, stop on overflow, then drain without accumulating."""
    if stream is None:
        raise GitError("Git child stream is unavailable")
    chunks: list[bytes] = []
    byte_count = 0
    exceeded = False
    while True:
        if byte_limit is None or exceeded:
            chunk_limit = 64 * 1024
        else:
            chunk_limit = min(64 * 1024, byte_limit - byte_count + 1)
        chunk = await stream.read(chunk_limit)
        if not chunk:
            return b"".join(chunks), exceeded
        byte_count += len(chunk)
        if byte_limit is not None and byte_count > byte_limit:
            exceeded = True
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
        if not exceeded:
            chunks.append(chunk)


async def _write_git_stdin(
    stream: asyncio.StreamWriter | None,
    content: bytes | None,
) -> None:
    """Write exact child input while tolerating a concurrent bounded stop."""
    if content is None:
        return
    if stream is None:
        raise GitError("Git child stdin is unavailable")
    try:
        stream.write(content)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await stream.wait_closed()


async def _await_bounded_git_cleanup(
    process: asyncio.subprocess.Process,
    completion: asyncio.Task[tuple[tuple[bytes, bool], tuple[bytes, bool], int]],
) -> None:
    """Stop one bounded child, drain both pipes, and reap despite cancellation."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001 - cleanup consumes local child failures
            break
    if not completion.cancelled():
        with suppress(BaseException):
            completion.result()


async def _run_git(
    args: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run a git command asynchronously and return stdout."""
    stdout = await run_git_bytes(args, cwd=cwd, env=env)
    return stdout.decode().strip()


async def run_git_bytes(
    args: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    *,
    stdin_bytes: bytes | None = None,
    stdin_descriptor: int | None = None,
    stdout_bytes_max: int | None = None,
    stderr_bytes_max: int | None = None,
    accepted_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    """Run a git command and return raw stdout bytes without decoding.

    Text and bytes runners share one executable, one sanitized environment,
    one timeout, one kill-and-drain cancellation path, and one error path.
    NUL-delimited stream output such as ``-z`` listings must stay raw:
    decoding or stripping here would corrupt filenames that contain leading
    whitespace or non-UTF-8 bytes.
    """
    if not args:
        raise ValueError("args must not be empty")
    if stdin_bytes is not None and type(stdin_bytes) is not bytes:
        raise TypeError("stdin_bytes must be bytes or None")
    if stdin_descriptor is not None and (type(stdin_descriptor) is not int or stdin_descriptor < 0):
        raise TypeError("stdin_descriptor must be a nonnegative integer or None")
    if stdin_bytes is not None and stdin_descriptor is not None:
        raise ValueError("stdin_bytes and stdin_descriptor are mutually exclusive")
    if stdout_bytes_max is not None and (type(stdout_bytes_max) is not int or stdout_bytes_max < 0):
        raise ValueError("stdout_bytes_max must be a nonnegative integer or None")
    if stderr_bytes_max is not None and (type(stderr_bytes_max) is not int or stderr_bytes_max < 0):
        raise ValueError("stderr_bytes_max must be a nonnegative integer or None")
    if (
        type(accepted_returncodes) is not tuple
        or not accepted_returncodes
        or any(type(code) is not int or code < 0 or code > 255 for code in accepted_returncodes)
        or len(set(accepted_returncodes)) != len(accepted_returncodes)
    ):
        raise ValueError("accepted_returncodes must contain unique process return codes")
    modeled = not sys.platform.startswith("linux")
    logger.debug("Running git operation %s", args[0])
    child_env = {
        "GCM_INTERACTIVE": "Never",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": "/usr/bin:/bin",
    }
    if env is not None:
        child_env.update(env)
    with ExitStack() as stack:
        stable_cwd: _StableDirectory | None
        if cwd is None:
            stable_cwd = None
        elif isinstance(cwd, _StableDirectory):
            stable_cwd = cwd
        else:
            stable_cwd = stack.enter_context(_retain_directory(cwd))
        stable_arguments = [argument for argument in args if isinstance(argument, _StableDirectory)]
        descriptors = tuple(
            dict.fromkeys(
                directory.descriptor
                for directory in [stable_cwd, *stable_arguments]
                if directory is not None
            )
        )
        command_args = [
            (
                argument.process_path(modeled=modeled)
                if isinstance(argument, _StableDirectory)
                else argument
            )
            for argument in args
        ]
        cmd = [_GIT_EXECUTABLE_PATH, *command_args]
        process_cwd = None if stable_cwd is None else stable_cwd.process_path(modeled=modeled)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=process_cwd,
            env=child_env,
            pass_fds=descriptors,
            stdin=(
                stdin_descriptor
                if stdin_descriptor is not None
                else (asyncio.subprocess.PIPE if stdin_bytes is not None else None)
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if stdout_bytes_max is None and stderr_bytes_max is None:
            try:
                communication = (
                    proc.communicate() if stdin_bytes is None else proc.communicate(stdin_bytes)
                )
                stdout, _stderr = await asyncio.wait_for(
                    communication,
                    timeout=_GIT_COMMAND_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                await _terminate_and_drain_git_process(proc)
                raise
            except TimeoutError as exc:
                await _terminate_and_drain_git_process(proc)
                raise GitError(f"git {args[0]} timed out") from exc
        else:
            stderr_limit = 16 * 1024 if stderr_bytes_max is None else stderr_bytes_max

            async def collect_bounded_streams() -> (
                tuple[tuple[bytes, bool], tuple[bytes, bool], int]
            ):
                stdout_result, stderr_result, returncode, _stdin_result = await asyncio.gather(
                    _read_bounded_git_stream(
                        proc.stdout,
                        byte_limit=stdout_bytes_max,
                        process=proc,
                    ),
                    _read_bounded_git_stream(
                        proc.stderr,
                        byte_limit=stderr_limit,
                        process=proc,
                    ),
                    proc.wait(),
                    _write_git_stdin(proc.stdin, stdin_bytes),
                )
                return stdout_result, stderr_result, returncode

            completion = asyncio.create_task(collect_bounded_streams())
            try:
                (
                    (stdout, stdout_exceeded),
                    (_stderr, stderr_exceeded),
                    _returncode,
                ) = await asyncio.wait_for(
                    asyncio.shield(completion),
                    timeout=_GIT_COMMAND_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                await _await_bounded_git_cleanup(proc, completion)
                raise
            except TimeoutError as exc:
                await _await_bounded_git_cleanup(proc, completion)
                raise GitError(f"git {args[0]} timed out") from exc
            if stdout_exceeded or stderr_exceeded:
                raise GitError(f"git {args[0]} output exceeded limit")
        if proc.returncode not in accepted_returncodes:
            logger.error("Git operation %s failed", args[0])
            raise GitError(f"git {args[0]} failed")
        if stable_cwd is not None and not _stable_directory_name_matches(stable_cwd):
            raise GitError("Git working directory changed during operation")
        return stdout


def _normalize_remote_url_for_compare(url: str) -> str:
    """Normalize a remote URL enough to compare logical equality."""
    if not url:
        raise ValueError("url must not be empty")
    normalized_url = url.strip()
    if not normalized_url:
        raise ValueError("url must not be blank")
    normalized_url = normalized_url.removesuffix(".git")
    return normalized_url.rstrip("/")


def _remote_urls_match(existing_remote_url: str, expected_remote_url: str) -> bool:
    """Return whether two remote URLs refer to the same repository."""
    if not isinstance(existing_remote_url, str):
        raise TypeError("existing_remote_url must be a string")
    if not isinstance(expected_remote_url, str):
        raise TypeError("expected_remote_url must be a string")
    if not existing_remote_url.strip():
        return False
    if not expected_remote_url.strip():
        raise ValueError("expected_remote_url must not be blank")
    return _normalize_remote_url_for_compare(
        existing_remote_url
    ) == _normalize_remote_url_for_compare(expected_remote_url)


async def _has_remote_refs(repo_path: str, remote_name: str = "origin") -> bool:
    """Return whether one local checkout has fetched refs for one remote."""
    if not isinstance(repo_path, str):
        raise TypeError("repo_path must be a string")
    if not isinstance(remote_name, str):
        raise TypeError("remote_name must be a string")
    normalized_repo_path = repo_path.strip()
    normalized_remote_name = remote_name.strip()
    if not normalized_repo_path:
        raise ValueError("repo_path must not be empty")
    if not normalized_remote_name:
        raise ValueError("remote_name must not be empty")

    refs_output = await _run_git(
        [
            "for-each-ref",
            "--format=%(refname)",
            f"refs/remotes/{normalized_remote_name}",
        ],
        cwd=normalized_repo_path,
    )
    for ref_name in refs_output.splitlines():
        normalized_ref_name = ref_name.strip()
        if not normalized_ref_name:
            continue
        if normalized_ref_name == f"refs/remotes/{normalized_remote_name}/HEAD":
            continue
        return True
    return False


async def get_remote_url(
    repo_path: str,
    remote_name: str = "origin",
) -> str | None:
    """Return one configured remote URL, or None when it is missing."""
    if not repo_path:
        raise ValueError("repo_path must not be empty")
    if not remote_name:
        raise ValueError("remote_name must not be empty")

    try:
        remote_url = await _run_git(
            ["remote", "get-url", remote_name],
            cwd=repo_path,
        )
    except GitError:
        return None

    normalized_remote_url = remote_url.strip()
    if not normalized_remote_url:
        return None
    return normalized_remote_url


async def ensure_remote_url(
    repo_path: str,
    remote_url: str,
    remote_name: str = "origin",
) -> bool:
    """Ensure a checkout points one named remote at the expected URL."""
    if not repo_path:
        raise ValueError("repo_path must not be empty")
    if not remote_name:
        raise ValueError("remote_name must not be empty")
    if not remote_url:
        raise ValueError("remote_url must not be empty")

    with _retain_directory_argument(repo_path) as repository:
        current_remote_url = await get_remote_url(repository, remote_name=remote_name)
        if current_remote_url is not None:
            if _normalize_remote_url_for_compare(
                current_remote_url
            ) == _normalize_remote_url_for_compare(remote_url):
                return False
            await _run_git(
                ["remote", "set-url", remote_name, remote_url],
                cwd=repository,
            )
            return True

        await _run_git(
            ["remote", "add", remote_name, remote_url],
            cwd=repository,
        )
        return True


async def clone_repo(
    url: str,
    dest: str,
    access_token: str | None = None,
    *,
    destination_must_be_absent: bool = False,
) -> str:
    """Clone a repository, with optional strict new-storage ownership."""
    _validate_clone_url(url)

    dest_path = Path(dest)
    if dest_path.exists():
        if destination_must_be_absent:
            raise GitError("Clone destination already exists")
        with _retain_directory_argument(dest) as destination:
            if await validate_local_repo(destination):
                # Verify the existing clone's remote matches the requested URL
                # before reusing it to prevent cross-repo data leakage.
                try:
                    existing_remote = await _run_git(
                        ["remote", "get-url", "origin"],
                        cwd=destination,
                    )
                except GitError:
                    existing_remote = ""
                if not _remote_urls_match(existing_remote, url):
                    raise GitError("Destination already contains a clone of a different repository")
                had_remote_refs_before_fetch = await _has_remote_refs(destination)
                logger.info("Reusing an existing repository clone")
                try:
                    with _git_askpass_env(access_token) as env:
                        await _run_git(["fetch", "--all"], cwd=destination, env=env)
                except GitError as error:
                    if not had_remote_refs_before_fetch:
                        raise GitError(
                            "Existing clone is incomplete and could not be refreshed"
                        ) from error
                    logger.warning("Repository refresh failed; reusing existing refs")
                    return dest
                if not had_remote_refs_before_fetch and not await _has_remote_refs(destination):
                    # The origin already matched and the fetch reached it, so zero
                    # refs mean a valid empty remote rather than a damaged clone.
                    logger.info("Reusing an existing clone of an empty remote repository")
                return dest
            raise GitError("Destination already exists but is not a git repository")
    with (
        _retain_new_directory(dest_path) as destination,
        _git_askpass_env(access_token) as env,
    ):
        await _run_git(["clone", url, "."], cwd=destination, env=env)
    logger.info("Repository clone completed")
    return dest


async def clone_local_repo(
    source: str,
    dest: str,
    remote_url: str | None = None,
    *,
    destination_must_be_absent: bool = False,
) -> str:
    """Clone a local git repository for tenant path repairs.

    Using the existing checkout as the clone source preserves local branches
    that may not have been pushed to the remote yet.
    """
    with _retain_directory_argument(source) as source_directory:
        if not await validate_local_repo(source_directory):
            raise GitError("Source repository is not a valid git repository")

        dest_path = Path(dest)
        if dest_path.exists():
            if destination_must_be_absent:
                raise GitError("Clone destination already exists")
            with _retain_directory_argument(dest) as destination:
                if not await validate_local_repo(destination):
                    raise GitError("Destination already exists but is not a git repository")
                if remote_url:
                    await _run_git(["remote", "set-url", "origin", remote_url], cwd=destination)
        else:
            with _retain_new_directory(dest_path) as destination:
                await _run_git(
                    ["clone", "--no-hardlinks", source_directory, "."],
                    cwd=destination,
                )
                if remote_url:
                    await _run_git(["remote", "set-url", "origin", remote_url], cwd=destination)

    logger.info("Local repository clone completed")
    return dest


async def resolve_remote_base_ref(
    repo_path: str,
    access_token: str | None = None,
) -> str:
    """Fetch origin and return the tracked default remote branch reference."""
    assert repo_path, "repo_path must not be empty"

    with _retain_directory_argument(repo_path) as repository:
        with _git_askpass_env(access_token) as env:
            await _run_git(["fetch", "origin"], cwd=repository, env=env)
            try:
                symbolic_ref = await _run_git(
                    ["symbolic-ref", "refs/remotes/origin/HEAD"],
                    cwd=repository,
                    env=env,
                )
            except GitError:
                symbolic_ref = ""

        normalized_symbolic_ref = symbolic_ref.strip()
        if normalized_symbolic_ref.startswith("refs/remotes/origin/"):
            remote_branch = normalized_symbolic_ref.removeprefix("refs/remotes/")
            assert remote_branch, "remote_branch must not be empty"
            return remote_branch

        for fallback_remote_branch in ("origin/main", "origin/master"):
            try:
                await _run_git(
                    ["rev-parse", "--verify", fallback_remote_branch],
                    cwd=repository,
                )
            except GitError:
                continue
            return fallback_remote_branch

    raise GitError("Could not determine the remote default branch")


async def _head_commit_exists(repo_path: str) -> bool:
    """Return whether HEAD resolves to a commit."""
    try:
        await _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo_path)
    except GitError:
        return False
    return True


async def _create_empty_root_commit(repo_path: str) -> str:
    """Create one empty root commit for a repository with an unborn branch.

    ``git worktree add`` cannot branch from an unborn HEAD, so a clone of an
    empty remote needs an explicit base commit. The commit carries the empty
    tree only, uses a fixed Yinshi identity, and leaves every existing branch
    untouched.
    """
    empty_tree_id = await _run_git(
        ["hash-object", "-w", "-t", "tree", os.devnull],
        cwd=repo_path,
    )
    commit_id = await _run_git(
        [
            "-c",
            "user.name=Yinshi",
            "-c",
            "user.email=noreply@yinshi.local",
            "commit-tree",
            empty_tree_id,
            "-m",
            "Initialize workspace on an empty repository",
        ],
        cwd=repo_path,
    )
    return commit_id


async def create_worktree(
    repo_path: str,
    worktree_path: str,
    branch: str,
    *,
    base_ref: str | None = None,
) -> str:
    """Create a git worktree with a new branch. Returns the worktree path."""
    assert repo_path, "repo_path must not be empty"
    assert worktree_path, "worktree_path must not be empty"
    assert branch, "branch must not be empty"

    with (
        _retain_directory_argument(repo_path) as repository,
        _retain_empty_worktree(worktree_path) as worktree,
    ):
        worktree_add_args = ["worktree", "add", "-b", branch, worktree]
        if base_ref is not None:
            normalized_base_ref = base_ref.strip()
            if not normalized_base_ref:
                raise ValueError("base_ref must not be empty when provided")
            worktree_add_args.append(normalized_base_ref)
        elif not await _head_commit_exists(repository):
            worktree_add_args.append(await _create_empty_root_commit(repository))
        await _run_git(worktree_add_args, cwd=repository)
        _require_stable_directory_name(worktree, object_name="worktree target")
    logger.info("Repository worktree created")
    return worktree_path


async def restore_worktree(repo_path: str, worktree_path: str, branch: str) -> str:
    """Restore a worktree for an existing branch, creating the branch if needed."""
    assert repo_path, "repo_path must not be empty"
    assert worktree_path, "worktree_path must not be empty"
    assert branch, "branch must not be empty"

    with _retain_directory_argument(repo_path) as repository:
        worktree_dir = Path(worktree_path)
        if worktree_dir.exists():
            with _retain_directory_argument(worktree_path) as existing_worktree:
                try:
                    os.stat(".git", dir_fd=existing_worktree.descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if await validate_local_repo(existing_worktree):
                        return worktree_path
        with _retain_empty_worktree(worktree_path) as worktree:
            try:
                await _run_git(["worktree", "add", worktree, branch], cwd=repository)
            except GitError:
                await _run_git(["worktree", "add", "-b", branch, worktree], cwd=repository)
            _require_stable_directory_name(worktree, object_name="worktree target")

    logger.info("Repository worktree restored")
    return worktree_path


async def delete_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove a git worktree and its branch."""
    with _retain_directory_argument(repo_path) as repository, ExitStack() as stack:
        try:
            existing_worktree = stack.enter_context(_retain_directory_argument(worktree_path))
        except GitError:
            existing_worktree = None
        branch = None
        if existing_worktree is not None:
            try:
                branch = await _run_git(
                    ["rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=existing_worktree,
                )
            except GitError:
                pass
            await _remove_worktree(repository, existing_worktree)
        else:
            await _remove_worktree(repository, worktree_path)

        if branch and branch not in ("main", "master"):
            try:
                await _run_git(["branch", "-D", branch], cwd=repository)
            except GitError:
                pass

    logger.info("Repository worktree deleted")


async def read_local_branch_oid(repo_path: str, branch: str) -> str | None:
    """Read one local branch generation before durable cleanup planning."""
    if not repo_path or not branch:
        raise ValueError("branch lookup values must not be empty")
    value = await _run_git(
        ["for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}"],
        cwd=repo_path,
    )
    oid = value.strip()
    if not oid:
        return None
    if re.fullmatch(r"[0-9a-f]{40,64}", oid) is None:
        raise GitError("Local branch identity is invalid")
    return oid


async def cleanup_repository_worktrees(
    repo_path: str,
    worktrees: Sequence[tuple[str, str] | tuple[str, str, str | None]],
) -> None:
    """Remove selected linked-worktree metadata and local branches after commit."""
    if not repo_path:
        raise ValueError("repo_path must not be empty")
    normalized: list[tuple[str, str, str | None]] = []
    for item in worktrees:
        if len(item) == 2:
            worktree_path, branch = item
            branch_oid: str | None = ""
        else:
            worktree_path, branch, branch_oid = item
        if not worktree_path or not branch:
            raise ValueError("worktree cleanup values must not be empty")
        if (
            branch_oid is not None
            and branch_oid != ""
            and re.fullmatch(r"[0-9a-f]{40,64}", branch_oid) is None
        ):
            raise ValueError("worktree cleanup branch identity is invalid")
        normalized.append((worktree_path, branch, branch_oid))

    first_error: GitError | None = None
    with _retain_directory_argument(repo_path) as repository:
        for worktree_path, _branch, _branch_oid in normalized:
            try:
                await _remove_worktree(repository, worktree_path)
            except GitError as exc:
                if first_error is None:
                    first_error = exc

        existing_refs: set[str] = set()
        try:
            refs_output = await _run_git(
                ["for-each-ref", "--format=%(refname)", "refs/heads"],
                cwd=repository,
            )
            existing_refs = set(refs_output.splitlines())
        except GitError as exc:
            if first_error is None:
                first_error = exc

        for _worktree_path, branch, branch_oid in normalized:
            if f"refs/heads/{branch}" not in existing_refs or branch_oid is None:
                continue
            try:
                if branch_oid:
                    await _run_git(
                        ["update-ref", "-d", f"refs/heads/{branch}", branch_oid],
                        cwd=repository,
                    )
                else:
                    await _run_git(["branch", "-D", "--", branch], cwd=repository)
            except GitError as exc:
                if first_error is None:
                    first_error = exc

    if first_error is not None:
        raise GitError("Repository worktree cleanup failed") from first_error
    logger.info("Repository worktrees cleaned up")


async def validate_local_repo(path: str) -> bool:
    """Check if a path is a valid git repository."""
    if not Path(path).exists():
        return False
    try:
        await _run_git(["rev-parse", "--git-dir"], cwd=path)
        return True
    except GitError:
        return False
