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
                raise GitError(f"Destination already contains a clone of a different repository")
            had_remote_refs_before_fetch = await _has_remote_refs(dest)
            logger.info("Reusing existing clone at %s", dest)
            try:
                with _git_askpass_env(access_token) as env:
                    await _run_git(["fetch", "--all"], cwd=dest, env=env)
            except GitError as error:
                if not had_remote_refs_before_fetch:
                    raise GitError(
                        "Existing clone is incomplete and could not be refreshed"
                    ) from error
                logger.warning(
                    "Fetch failed for existing clone at %s; reusing existing refs",
                    dest,
                )
                return dest
            if not had_remote_refs_before_fetch and not await _has_remote_refs(dest):
                raise GitError("Existing clone is incomplete and missing remote refs")
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
    await _run_git(worktree_add_args, cwd=repo_path)
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
