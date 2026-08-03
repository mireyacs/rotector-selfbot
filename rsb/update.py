"""Staying current, for a tool that is installed by cloning it.

There is no package to upgrade -- the install instructions are ``git clone``,
so the update *is* a fast-forward of the working copy. That shapes every
decision here:

* **Nothing happens without being asked.** Checking is a read: ``git fetch``
  touches no tracked file. Applying moves the code that is about to run, so it
  waits for the dialog to come back true.
* **Git has to actually be there.** A clone can be copied to a machine with no
  git, or run from a tarball with no ``.git`` at all, and shelling out to a
  missing binary would surface as a stack trace at startup. Every entry point
  checks first and reports what is missing in a sentence.
* **A dirty tree is never touched.** Local edits are somebody's work. The
  merge is ``--ff-only`` against an explicitly fetched upstream, so it either
  advances cleanly or refuses -- it will not invent a merge commit, and it
  cannot leave a half-resolved conflict in the tree of a running scanner.

Everything is a plain subprocess rather than a git library, because the one
thing this must not do is need its own dependency to update the thing that
installs dependencies.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: git is a network call away; none of these should hang the UI
FETCH_TIMEOUT = 25.0
LOCAL_TIMEOUT = 10.0

#: how many subjects to show in the dialog before summarising the rest
MAX_LISTED = 12


@dataclass
class UpdateStatus:
    """What one check found. Never raises; a failure is a ``reason``."""

    #: git is installed and this is a clone with an upstream
    usable: bool = False
    #: commits on the upstream that are not in the working copy
    behind: int = 0
    #: (short sha, subject), newest last
    commits: list[tuple[str, str]] = field(default_factory=list)
    branch: str = ""
    upstream: str = ""
    #: tracked files with local modifications
    dirty: bool = False
    #: why a check or an apply cannot proceed, in one sentence
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.usable and self.behind > 0

    @property
    def can_apply(self) -> bool:
        return self.available and not self.dirty

    def describe(self) -> str:
        if not self.usable:
            return self.reason or "Updates cannot be checked."
        if not self.behind:
            return f"Up to date with {self.upstream}."
        plural = "" if self.behind == 1 else "s"
        if self.dirty:
            return (
                f"{self.behind} new commit{plural} on {self.upstream}, but this "
                f"working copy has uncommitted changes."
            )
        return f"{self.behind} new commit{plural} on {self.upstream}."


def _env() -> dict:
    """The caller's environment, with prompting turned off.

    Inherited rather than replaced. A hand-built environment looks tidy and
    breaks git on Windows, where the process needs ``SYSTEMROOT`` to resolve
    anything and finds the user's home through ``USERPROFILE`` rather than
    ``HOME`` -- and it would silently drop proxy settings, credential helpers
    and ``GIT_*`` overrides on every platform.

    ``GIT_TERMINAL_PROMPT=0`` is the one thing overridden: a credential prompt
    on a headless fetch would hang the check with no way to answer it.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


async def _git(*args: str, cwd: Path = ROOT, timeout: float = LOCAL_TIMEOUT):
    """Run git, returning (returncode, stdout, stderr) and never raising."""
    binary = shutil.which("git")
    if binary is None:
        return 127, "", "git is not installed, or not on PATH"
    try:
        process = await asyncio.create_subprocess_exec(
            binary, *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(),
        )
    # NotImplementedError is the one Windows raises when the running event loop
    # cannot spawn subprocesses at all; it is not an OSError, and letting it out
    # would take the app down over a routine update check.
    except (OSError, NotImplementedError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"

    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        return 124, "", f"git {args[0]} timed out after {timeout:.0f}s"
    return (
        process.returncode or 0,
        # git speaks whatever the repository holds; decoding is best-effort
        # rather than a reason to fail an update check
        out.decode("utf-8", "replace").strip(),
        err.decode("utf-8", "replace").strip(),
    )


def git_available() -> bool:
    """True when a git binary can be found on PATH."""
    return shutil.which("git") is not None


def is_clone(root: Path = ROOT) -> bool:
    """True when ``root`` looks like a git working copy."""
    return (root / ".git").exists()


def preflight(root: Path = ROOT) -> str:
    """The reason updates are unavailable here, or "" when they are.

    Checked before any subprocess so the common "not installed from git" case
    costs nothing and reads as a fact rather than as a failure.
    """
    if not git_available():
        return (
            "git is not installed or not on PATH, so updates cannot be "
            "checked. Install git, or update by hand."
        )
    if not is_clone(root):
        return (
            f"{root} is not a git clone, so there is nothing to pull. "
            f"Re-install with git clone to get updates."
        )
    return ""


async def check(root: Path = ROOT) -> UpdateStatus:
    """Fetch and report how far behind the working copy is.

    Read-only: ``git fetch`` writes to ``.git`` and touches nothing tracked.
    """
    status = UpdateStatus()
    status.reason = preflight(root)
    if status.reason:
        return status

    code, branch, err = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if code:
        status.reason = f"Could not read the current branch: {err or 'git failed'}"
        return status
    status.branch = branch
    if branch == "HEAD":
        status.reason = (
            "This copy is on a detached HEAD rather than a branch, so there is "
            "no upstream to follow."
        )
        return status

    code, upstream, err = await _git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=root
    )
    if code:
        status.reason = (
            f"Branch {branch!r} is not tracking a remote branch, so there is "
            f"nothing to check against."
        )
        return status
    status.upstream = upstream

    remote = upstream.split("/", 1)[0]
    code, _, err = await _git("fetch", "--quiet", remote, cwd=root,
                              timeout=FETCH_TIMEOUT)
    if code:
        status.reason = f"Could not reach {remote}: {err or 'git fetch failed'}"
        return status

    code, count, err = await _git("rev-list", "--count", f"HEAD..{upstream}", cwd=root)
    if code:
        status.reason = f"Could not compare against {upstream}: {err or 'git failed'}"
        return status
    try:
        status.behind = int(count or "0")
    except ValueError:
        status.behind = 0

    if status.behind:
        code, log, _ = await _git(
            "log", "--no-merges", "--pretty=%h\t%s", f"HEAD..{upstream}", cwd=root
        )
        if not code and log:
            for line in log.splitlines():
                sha, _, subject = line.partition("\t")
                if sha:
                    status.commits.append((sha, subject))
            status.commits.reverse()  # oldest first, the order they land in

    code, porcelain, _ = await _git("status", "--porcelain", "--untracked-files=no",
                                    cwd=root)
    status.dirty = bool(not code and porcelain)
    status.usable = True
    return status


async def apply(root: Path = ROOT) -> tuple[bool, str]:
    """Fast-forward the working copy. Returns (ok, message).

    Re-checks rather than trusting the status the dialog was built from: the
    tree can be edited, and the branch can move, between the check and the
    confirmation.
    """
    reason = preflight(root)
    if reason:
        return False, reason

    status = await check(root)
    if not status.usable:
        return False, status.reason
    if not status.behind:
        return True, f"Already up to date with {status.upstream}."
    if status.dirty:
        return False, (
            "This working copy has uncommitted changes to tracked files. "
            "Commit or stash them first -- updating would not be able to keep "
            "both your edits and the new commits."
        )

    # --ff-only, so this either advances the branch or declines. It will not
    # write a merge commit, and it cannot leave a conflicted tree behind.
    code, _, err = await _git("merge", "--ff-only", status.upstream, cwd=root)
    if code:
        return False, (
            f"Could not fast-forward to {status.upstream}: "
            f"{err or 'git merge failed'}. Your working copy is unchanged."
        )

    plural = "" if status.behind == 1 else "s"
    return True, (
        f"Updated to {status.upstream}: {status.behind} commit{plural} applied."
    )


def requirements_changed(status: UpdateStatus) -> bool:
    """Whether the update's subjects suggest dependencies moved.

    A heuristic on purpose. It only decides whether to add "check
    requirements.txt" to a message, and being wrong costs a sentence.
    """
    words = ("requirement", "dependency", "dependencies", "bump", "pillow",
             "textual", "httpx", "websockets")
    return any(
        any(word in subject.lower() for word in words) for _, subject in status.commits
    )
