"""Git subprocess wrappers for the space refresh pipeline.

Thin functions that shell out to git via subprocess.run in argv form (no shell
interpolation). Each function logs the command at INFO level and handles
timeouts and exit codes according to its contract.

Each raises GitCommandError on non-zero exit and lets subprocess.TimeoutExpired
propagate.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from collections.abc import Sequence

from .errors import GitCommandError
from ..core.models import ChangeType, FileChange

logger = logging.getLogger(__name__)


def run_git(argv: Sequence[str], **kwargs) -> "subprocess.CompletedProcess[str]":
    """Run a git command given as an argv sequence (never a shell string)."""
    # argv form with no shell=True, so there is no shell to inject into.
    # External git URLs are validated by validate_git_url (scheme allowlist
    # plus fail-closed host allowlist) before reaching git, and the clone
    # positionals are guarded with a "--" end-of-options separator.
    return subprocess.run([*argv], **kwargs)  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit


def redact_git_url(url: str | None) -> str:
    """Strip embedded credentials (userinfo) from a git URL for safe display.

    Examples:
        https://ghp_TOKEN@github.com/org/repo.git → https://github.com/org/repo.git
        https://user:pass@github.com/org/repo.git → https://github.com/org/repo.git
        https://github.com/org/repo.git           → https://github.com/org/repo.git  (no-op)
    """
    if not url:
        return url or ""
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            # Rebuild netloc without userinfo
            host_port = parsed.hostname or ""
            if parsed.port:
                host_port += f":{parsed.port}"
            cleaned = parsed._replace(netloc=host_port)
            return urlunparse(cleaned)
    except Exception:
        pass
    return url


# Userinfo (token, user:pass) of any URL in free text such as git's stderr.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@'\"]+@")


def redact_git_credentials(text: str | None) -> str:
    """Strip embedded credentials from every URL in ``text``."""
    return _URL_USERINFO.sub(r"\1", text or "")


def _git_failed(command: str, result: "subprocess.CompletedProcess[str]") -> GitCommandError:
    # Some git versions and servers echo the remote URL, token included, in stderr.
    return GitCommandError(
        command=command,
        exit_code=result.returncode,
        stderr=redact_git_credentials(result.stderr.strip()),
    )


def _resolve_env(env: dict[str, str] | None) -> dict[str, str]:
    """Env for a git subprocess: the caller's (with an injected credential) or
    the hardened credential-free default."""
    if env is not None:
        return env
    from ..service.codekb_service import _GIT_SUBPROCESS_ENV

    return _GIT_SUBPROCESS_ENV


def clone_shallow(
    git_url: str,
    branch: str | None,
    dest: Path,
    timeout_s: int = 300,
    env: dict[str, str] | None = None,
) -> str:
    """Clone a repository at depth 1 on a single branch.

    Runs: git clone --depth 1 [--branch <branch>] <url> <dest>

    If branch is None or empty, the --branch flag is omitted and git
    clones the remote's default branch (matching the initial-analysis
    behaviour for spaces created without an explicit branch).

    Args:
        git_url: The remote repository URL.
        branch: The branch to clone, or None for the remote default.
        dest: Local directory to clone into (must not already exist).
        timeout_s: Subprocess timeout in seconds.

    Returns:
        The HEAD commit SHA of the freshly cloned repository.

    Raises:
        GitCommandError: If the clone exits with a non-zero status.
        subprocess.TimeoutExpired: If the clone exceeds timeout_s.
    """
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", git_url, str(dest)]
    # The URL can carry a token: redact it in the log line and error text; git gets the real URL.
    shown = " ".join([*cmd[:-2], redact_git_url(git_url), str(dest)])
    logger.info("git_ops.clone_shallow: %s", shown)

    result = run_git(
        cmd, capture_output=True, text=True, timeout=timeout_s, env=_resolve_env(env)
    )
    if result.returncode != 0:
        raise _git_failed(shown, result)

    # Read HEAD SHA from the cloned repo
    rev_cmd = ["git", "-C", str(dest), "rev-parse", "HEAD"]
    rev_result = run_git(rev_cmd, capture_output=True, text=True, timeout=30)
    if rev_result.returncode != 0:
        raise _git_failed(" ".join(rev_cmd), rev_result)

    return rev_result.stdout.strip()


def fetch_specific_sha(
    repo_dir: Path,
    remote: str,
    sha: str,
    timeout_s: int = 120,
    env: dict[str, str] | None = None,
) -> None:
    """Fetch a specific commit SHA at depth 1.

    Runs: git -C <repo_dir> fetch --depth 1 <remote> <sha>

    Args:
        repo_dir: Path to the local repository.
        remote: Remote name (typically "origin").
        sha: The commit SHA to fetch.
        timeout_s: Subprocess timeout in seconds.

    Raises:
        GitCommandError: If the fetch exits non-zero (commit gone, server
            refuses the SHA, auth or network failure); git's stderr says which.
        subprocess.TimeoutExpired: If the fetch exceeds timeout_s.
    """
    cmd = ["git", "-C", str(repo_dir), "fetch", "--depth", "1", remote, sha]
    logger.info("git_ops.fetch_specific_sha: %s", " ".join(cmd))

    result = run_git(
        cmd, capture_output=True, text=True, timeout=timeout_s, env=_resolve_env(env)
    )
    if result.returncode != 0:
        raise _git_failed(" ".join(cmd), result)


def diff_name_status(
    repo_dir: Path,
    prior_sha: str,
    new_sha: str,
    timeout_s: int = 60,
) -> list[FileChange]:
    """Compute file-level changes between two commits.

    Runs: git -C <repo_dir> diff --name-status -z <prior_sha>..<new_sha>

    Parses NUL-separated records of the form:
        <STATUS>\\0<path>
        R<similarity>\\0<old_path>\\0<new_path>   (renames)
        C<similarity>\\0<old_path>\\0<new_path>   (copies)

    Rename entries are expanded into REMOVED(old_path) + ADDED(new_path).
    Copy entries produce ADDED(new_path) only (the source still exists).

    Args:
        repo_dir: Path to the local repository.
        prior_sha: The base commit SHA.
        new_sha: The target commit SHA.
        timeout_s: Subprocess timeout in seconds.

    Returns:
        A list of FileChange objects representing all changed files.

    Raises:
        GitCommandError: If git diff exits with a non-zero status.
        subprocess.TimeoutExpired: If the diff exceeds timeout_s.
    """
    cmd = [
        "git",
        "-C",
        str(repo_dir),
        "diff",
        "--name-status",
        # Without -z git C-quotes any path with a backslash, quote or non-ASCII byte.
        "-z",
        f"{prior_sha}..{new_sha}",
    ]
    logger.info("git_ops.diff_name_status: %s", " ".join(cmd))

    # surrogateescape decodes non-UTF-8 names as os.walk does, instead of raising.
    result = run_git(
        cmd, capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=timeout_s
    )
    if result.returncode != 0:
        raise _git_failed(" ".join(cmd), result)

    changes: list[FileChange] = []
    fields = result.stdout.split("\0")
    i = 0
    while i < len(fields):
        status_code = fields[i].strip()
        i += 1
        if not status_code:
            continue
        first_char = status_code[0].upper()
        path_count = 2 if first_char in ("R", "C") else 1
        paths = fields[i : i + path_count]
        i += path_count
        if len(paths) < path_count or not all(paths):
            logger.warning(
                "git_ops.diff_name_status: skipping malformed record: %r", [status_code, *paths]
            )
            continue
        if first_char == "R":
            # Rename: REMOVED(old) + ADDED(new)
            changes.append(FileChange(path=paths[0], change_type=ChangeType.REMOVED))
            changes.append(FileChange(path=paths[1], change_type=ChangeType.ADDED))
        elif first_char == "C":
            # Copy: only the new path is ADDED (source still exists)
            changes.append(FileChange(path=paths[1], change_type=ChangeType.ADDED))
        else:
            # A, M, D, T — single path
            change_type = ChangeType.from_git_status(status_code)
            changes.append(FileChange(path=paths[0], change_type=change_type))
    return changes
