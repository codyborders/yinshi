"""Git operations: clone repos and manage worktrees."""

import asyncio
import logging
import os
import secrets
import string
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
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
        except BaseException:
            break
    if not drain_task.cancelled():
        with suppress(BaseException):
            drain_task.result()


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
    cmd = [_GIT_EXECUTABLE_PATH, *args]
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
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_GIT_COMMAND_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        await _terminate_and_drain_git_process(proc)
        raise
    except TimeoutError as exc:
        await _terminate_and_drain_git_process(proc)
        raise GitError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        logger.error("Git operation %s failed", args[0])
        raise GitError(f"git {args[0]} failed")
    return stdout


def _normalize_remote_url_for_compare(url: str) -> str:
    """Normalize a remote URL enough to compare logical equality."""
    if not url:
        raise ValueError("url must not be empty")
    normalized_url = url.strip()
    if not normalized_url:
        raise ValueError("url must not be blank")
    if normalized_url.endswith(".git"):
        normalized_url = normalized_url[:-4]
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

    current_remote_url = await get_remote_url(repo_path, remote_name=remote_name)
    if current_remote_url is not None:
        if _normalize_remote_url_for_compare(
            current_remote_url
        ) == _normalize_remote_url_for_compare(remote_url):
            return False
        await _run_git(
            ["remote", "set-url", remote_name, remote_url],
            cwd=repo_path,
        )
        return True

    await _run_git(
        ["remote", "add", remote_name, remote_url],
        cwd=repo_path,
    )
    return True


async def clone_repo(
    url: str,
    dest: str,
    access_token: str | None = None,
) -> str:
    """Clone a git repository. Returns the clone path.

    If dest already exists and is a valid git repo with matching remote, reuse it.
    """
    _validate_clone_url(url)

    dest_path = Path(dest)
    if dest_path.exists():
        if await validate_local_repo(dest):
            # Verify the existing clone's remote matches the requested URL
            # before reusing it to prevent cross-repo data leakage.
            try:
                existing_remote = await _run_git(
                    ["remote", "get-url", "origin"],
                    cwd=dest,
                )
            except GitError:
                existing_remote = ""
            if not _remote_urls_match(existing_remote, url):
                raise GitError("Destination already contains a clone of a different repository")
            had_remote_refs_before_fetch = await _has_remote_refs(dest)
            logger.info("Reusing an existing repository clone")
            try:
                with _git_askpass_env(access_token) as env:
                    await _run_git(["fetch", "--all"], cwd=dest, env=env)
            except GitError as error:
                if not had_remote_refs_before_fetch:
                    raise GitError(
                        "Existing clone is incomplete and could not be refreshed"
                    ) from error
                logger.warning("Repository refresh failed; reusing existing refs")
                return dest
            if not had_remote_refs_before_fetch and not await _has_remote_refs(dest):
                # The origin already matched and the fetch reached it, so zero
                # refs mean a valid empty remote rather than a damaged clone.
                logger.info("Reusing an existing clone of an empty remote repository")
            return dest
        raise GitError("Destination already exists but is not a git repository")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with _git_askpass_env(access_token) as env:
        await _run_git(["clone", url, dest], env=env)
    logger.info("Repository clone completed")
    return dest


async def clone_local_repo(
    source: str,
    dest: str,
    remote_url: str | None = None,
) -> str:
    """Clone a local git repository for tenant path repairs.

    Using the existing checkout as the clone source preserves local branches
    that may not have been pushed to the remote yet.
    """
    if not await validate_local_repo(source):
        raise GitError("Source repository is not a valid git repository")

    dest_path = Path(dest)
    if dest_path.exists():
        if not await validate_local_repo(dest):
            raise GitError("Destination already exists but is not a git repository")
    else:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        await _run_git(["clone", "--no-hardlinks", source, dest])

    if remote_url:
        await _run_git(["remote", "set-url", "origin", remote_url], cwd=dest)

    logger.info("Local repository clone completed")
    return dest


async def resolve_remote_base_ref(
    repo_path: str,
    access_token: str | None = None,
) -> str:
    """Fetch origin and return the tracked default remote branch reference."""
    assert repo_path, "repo_path must not be empty"

    with _git_askpass_env(access_token) as env:
        await _run_git(["fetch", "origin"], cwd=repo_path, env=env)
        try:
            symbolic_ref = await _run_git(
                ["symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=repo_path,
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
                cwd=repo_path,
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

    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    worktree_add_args = ["worktree", "add", "-b", branch, worktree_path]
    if base_ref is not None:
        normalized_base_ref = base_ref.strip()
        if not normalized_base_ref:
            raise ValueError("base_ref must not be empty when provided")
        worktree_add_args.append(normalized_base_ref)
    elif not await _head_commit_exists(repo_path):
        worktree_add_args.append(await _create_empty_root_commit(repo_path))
    await _run_git(worktree_add_args, cwd=repo_path)
    logger.info("Repository worktree created")
    return worktree_path


async def restore_worktree(repo_path: str, worktree_path: str, branch: str) -> str:
    """Restore a worktree for an existing branch, creating the branch if needed."""
    assert repo_path, "repo_path must not be empty"
    assert worktree_path, "worktree_path must not be empty"
    assert branch, "branch must not be empty"

    worktree_dir = Path(worktree_path)
    if worktree_dir.exists():
        if await validate_local_repo(worktree_path):
            return worktree_path
        raise GitError("Worktree path already exists but is not a git repository")

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _run_git(["worktree", "add", worktree_path, branch], cwd=repo_path)
    except GitError:
        await _run_git(["worktree", "add", "-b", branch, worktree_path], cwd=repo_path)

    logger.info("Repository worktree restored")
    return worktree_path


async def delete_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove a git worktree and its branch."""
    try:
        branch = await _run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
        )
    except GitError:
        branch = None

    await _run_git(["worktree", "remove", "--force", worktree_path], cwd=repo_path)

    if branch and branch not in ("main", "master"):
        try:
            await _run_git(["branch", "-D", branch], cwd=repo_path)
        except GitError:
            pass

    logger.info("Repository worktree deleted")


async def cleanup_repository_worktrees(
    repo_path: str,
    worktrees: list[tuple[str, str]],
) -> None:
    """Remove selected linked-worktree metadata and local branches after commit."""
    if not repo_path:
        raise ValueError("repo_path must not be empty")
    for worktree_path, branch in worktrees:
        if not worktree_path or not branch:
            raise ValueError("worktree cleanup values must not be empty")

    first_error: GitError | None = None
    for worktree_path, _branch in worktrees:
        try:
            await _run_git(
                ["worktree", "remove", "--force", worktree_path],
                cwd=repo_path,
            )
        except GitError as exc:
            if first_error is None:
                first_error = exc

    existing_refs: set[str] = set()
    try:
        refs_output = await _run_git(
            ["for-each-ref", "--format=%(refname)", "refs/heads"],
            cwd=repo_path,
        )
        existing_refs = set(refs_output.splitlines())
    except GitError as exc:
        if first_error is None:
            first_error = exc

    for _worktree_path, branch in worktrees:
        if f"refs/heads/{branch}" not in existing_refs:
            continue
        try:
            await _run_git(["branch", "-D", "--", branch], cwd=repo_path)
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
