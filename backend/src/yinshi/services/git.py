"""Git operations: clone repos and manage worktrees."""

import asyncio
import logging
import os
import random
import string
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from yinshi.exceptions import GitError

logger = logging.getLogger(__name__)

_ADJECTIVES = [
    "swift", "bold", "calm", "dark", "keen", "warm", "cool", "pure", "wise", "fast",
    "bright", "quiet", "sharp", "smooth", "steady", "gentle", "vivid", "grand", "noble",
    "fresh", "prime", "lunar", "solar", "amber", "coral", "ivory", "olive", "azure",
]
_NOUNS = [
    "fox", "owl", "elk", "wolf", "hawk", "bear", "lynx", "crane", "drake", "finch",
    "heron", "raven", "otter", "tiger", "eagle", "falcon", "panda", "bison", "cedar",
    "maple", "river", "stone", "flame", "frost", "storm", "ridge", "grove", "brook",
]

_ALLOWED_URL_SCHEMES = ("https://", "ssh://", "git@")


def generate_branch_name(username: str | None = None) -> str:
    """Generate a random branch name like 'username/swift-fox-a3f2'."""
    adj = random.choice(_ADJECTIVES)
    noun = random.choice(_NOUNS)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    bare = f"{adj}-{noun}-{suffix}"
    if username:
        return f"{username}/{bare}"
    return bare


def _validate_clone_url(url: str) -> None:
    """Reject dangerous git URL schemes."""
    if url.startswith("-"):
        raise GitError("Invalid repository URL")
    if url.startswith(("ext::", "file://")):
        raise GitError("URL scheme not allowed")
    if not any(url.startswith(s) for s in _ALLOWED_URL_SCHEMES):
        raise GitError("URL must start with https://, ssh://, or git@")


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
            "case \"$1\" in\n"
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


async def _run_git(
    args: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run a git command asynchronously and return stdout."""
    cmd = ["git"] + args
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd)
    child_env = os.environ.copy()
    if env is not None:
        child_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("git %s failed (cwd=%s): %s", args[0], cwd, stderr.decode().strip())
        raise GitError(f"git {args[0]} failed")
    return stdout.decode().strip()


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
                    ["remote", "get-url", "origin"], cwd=dest,
                )
            except GitError:
                existing_remote = ""
            if existing_remote.strip().rstrip("/") != url.strip().rstrip("/"):
                raise GitError(
                    f"Destination already contains a clone of a different repository"
                )
            logger.info("Reusing existing clone at %s", dest)
            try:
                with _git_askpass_env(access_token) as env:
                    await _run_git(["fetch", "--all"], cwd=dest, env=env)
            except GitError:
                pass
            return dest
        raise GitError("Destination already exists but is not a git repository")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with _git_askpass_env(access_token) as env:
        await _run_git(["clone", url, dest], env=env)
    logger.info("Cloned %s to %s", url, dest)
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

    logger.info("Cloned local repo %s to %s", source, dest)
    return dest


async def create_worktree(repo_path: str, worktree_path: str, branch: str) -> str:
    """Create a git worktree with a new branch. Returns the worktree path."""
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    await _run_git(["worktree", "add", "-b", branch, worktree_path], cwd=repo_path)
    logger.info("Created worktree %s (branch: %s)", worktree_path, branch)
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

    logger.info("Restored worktree %s (branch: %s)", worktree_path, branch)
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

    logger.info("Deleted worktree %s", worktree_path)


async def validate_local_repo(path: str) -> bool:
    """Check if a path is a valid git repository."""
    if not Path(path).exists():
        return False
    try:
        await _run_git(["rev-parse", "--git-dir"], cwd=path)
        return True
    except GitError:
        return False
