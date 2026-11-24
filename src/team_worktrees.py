"""Provision isolated, local Git worktrees for team participants editing code."""
import hashlib
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt


_TIMEOUT = 20
_REVISION = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class WorktreeError(RuntimeError):
    """A safe team worktree could not be provisioned."""


def _git(git_path, cwd, *args, allow=(0,), hooks_path=None):
    if not isinstance(git_path, str) or not os.path.isabs(git_path) or not os.path.isfile(git_path) or not os.access(git_path, os.X_OK):
        raise WorktreeError("git_path must be an absolute path to an executable git")
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "HOME": os.environ.get("HOME", "/")}
    try:
        command = [git_path, "-C", str(cwd)]
        if hooks_path is not None:
            command.extend(("-c", "core.hooksPath={}".format(hooks_path)))
        result = subprocess.run([*command, *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=_TIMEOUT, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError("git worktree operation failed: {}".format(exc)) from exc
    if result.returncode not in allow:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise WorktreeError("git {} failed ({}): {}".format(args[0], result.returncode, detail))
    return result.returncode, result.stdout.decode("utf-8", "replace").strip()


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _owned_path(state_dir, run_id, participant_id):
    return Path(state_dir) / "worktrees" / _digest(run_id) / _digest(participant_id)


@contextmanager
def _lock(state_dir):
    lock = Path(state_dir) / ".team-worktrees.lock"
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + _TIMEOUT
    try:
        while True:
            try:
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover - exercised on Windows
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise WorktreeError("timed out waiting for team worktree lock")
                time.sleep(0.05)
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - exercised on Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _common_dir(git_path, repo, hooks_path):
    _, raw = _git(git_path, repo, "rev-parse", "--git-common-dir", hooks_path=hooks_path)
    path = Path(raw)
    return (path if path.is_absolute() else repo / path).resolve()


def _verify_existing(git_path, target, common, branch, base, hooks_path):
    if target.is_symlink() or not target.is_dir():
        raise WorktreeError("owned worktree path exists but is not a directory: {}".format(target))
    if not os.path.lexists(target / ".git"):
        raise WorktreeError("owned worktree path exists but is not a Git worktree: {}".format(target))
    _, top = _git(git_path, target, "rev-parse", "--show-toplevel", hooks_path=hooks_path)
    if Path(top).resolve() != target.resolve():
        raise WorktreeError("existing worktree resolves to a different checkout: {}".format(target))
    if _common_dir(git_path, target, hooks_path) != common:
        raise WorktreeError("existing worktree belongs to a different Git repository")
    _, current_branch = _git(git_path, target, "symbolic-ref", "--quiet", "--short", "HEAD", hooks_path=hooks_path)
    if current_branch != branch:
        raise WorktreeError("existing worktree is on unexpected branch {}".format(current_branch))
    rc, _ = _git(git_path, target, "merge-base", "--is-ancestor", base, "HEAD", allow=(0, 1), hooks_path=hooks_path)
    if rc == 1:
        raise WorktreeError("existing worktree does not descend from requested base revision")
    _, head = _git(git_path, target, "rev-parse", "HEAD", hooks_path=hooks_path)
    return head


def provision_edit_worktree(source_repo, state_dir, run_id, participant_id, base_commit, git_path):
    """Create or verify the owned participant worktree and return its execution context.

    Research and read-only tasks should not call this function. The source checkout is never
    reset, checked out, committed, merged, or pushed; Git only gains a linked worktree and a
    local branch. Existing owned worktrees are reused without moving their HEAD.
    """
    if not isinstance(run_id, str) or not run_id or not isinstance(participant_id, str) or not participant_id:
        raise WorktreeError("run_id and participant_id must be nonempty strings")
    if not isinstance(base_commit, str) or not _REVISION.fullmatch(base_commit):
        raise WorktreeError("base_commit must be a full 40- or 64-character commit SHA")
    if not isinstance(source_repo, (str, os.PathLike)) or not Path(source_repo).is_absolute():
        raise WorktreeError("source_repo must be an absolute path to the canonical checkout root")
    source_input = Path(source_repo)
    try:
        source = source_input.resolve(strict=True)
    except OSError as exc:
        raise WorktreeError("source repository does not exist: {}".format(source_input)) from exc
    if not source.is_dir():
        raise WorktreeError("source_repo is not a directory")
    if not isinstance(git_path, str) or not os.path.isabs(git_path) or not os.path.isfile(git_path) or not os.access(git_path, os.X_OK):
        raise WorktreeError("git_path must be an absolute path to an executable git")

    try:
        state = Path(state_dir).expanduser().absolute().resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise WorktreeError("state_dir must be a usable filesystem path") from exc
    if state == source or state.is_relative_to(source):
        raise WorktreeError("state_dir must be outside the source checkout")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    worktrees = state / "worktrees"
    worktrees.mkdir(mode=0o700, exist_ok=True)
    if worktrees.is_symlink() or not worktrees.is_dir():
        raise WorktreeError("worktrees state path must be a real directory")
    os.chmod(worktrees, 0o700)
    hooks_path = state / ".team-empty-hooks"
    hooks_path.mkdir(mode=0o700, exist_ok=True)
    if hooks_path.is_symlink() or not hooks_path.is_dir() or any(hooks_path.iterdir()):
        raise WorktreeError("hook suppression path must be an empty directory")
    target = _owned_path(state, run_id, participant_id)
    run_key, participant_key = _digest(run_id), _digest(participant_id)
    branch = "relay/team/{}/{}".format(run_key, participant_key)

    _, top = _git(git_path, source, "rev-parse", "--show-toplevel", hooks_path=hooks_path)
    if Path(top).resolve() != source:
        raise WorktreeError("source_repo must name the canonical Git checkout root")
    common = _common_dir(git_path, source, hooks_path)
    _, base = _git(git_path, source, "rev-parse", "--verify", base_commit + "^{commit}", hooks_path=hooks_path)
    if not _REVISION.fullmatch(base):
        raise WorktreeError("git did not resolve base_commit to a full commit SHA")

    with _lock(state):
        if target.parent.is_symlink():
            raise WorktreeError("owned worktree parent must not be a symlink")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(worktrees.resolve()):
            raise WorktreeError("owned worktree parent escapes configured state directory")
        if os.path.lexists(target):
            _verify_existing(git_path, target, common, branch, base, hooks_path)
        else:
            rc, _ = _git(git_path, source, "show-ref", "--verify", "--quiet", "refs/heads/" + branch,
                         allow=(0, 1), hooks_path=hooks_path)
            if rc == 0:
                raise WorktreeError("expected branch already exists without its owned worktree")
            _git(git_path, source, "worktree", "add", "--quiet", "-b", branch, str(target), base,
                 hooks_path=hooks_path)
            os.chmod(target, 0o700)
            _verify_existing(git_path, target, common, branch, base, hooks_path)
    return {"cwd": str(target), "branch": branch, "base_revision": base}
