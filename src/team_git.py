"""Shared local Git invocation and file locking for team records.

Team worktree provisioning and history export both shell out to a trusted Git
binary and serialize access with an exclusive lock file. Keeping that logic in
one place means the environment scrub, timeouts and Windows fallback cannot
drift between the two callers.
"""
import os
import subprocess
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt


class GitCommandError(RuntimeError):
    """A trusted Git invocation failed, timed out, or could not start."""

    def __init__(self, command, code, detail):
        super().__init__(detail)
        self.command = command
        self.code = code
        self.detail = detail


class LockTimeout(TimeoutError):
    """Another holder kept the advisory lock for the whole timeout."""


def executable(git_path):
    """Return the path when it is an absolute, executable file, else None."""
    if (not isinstance(git_path, str) or not os.path.isabs(git_path)
            or not os.path.isfile(git_path) or not os.access(git_path, os.X_OK)):
        return None
    return git_path


def environment(home=None):
    """The minimal environment a trusted Git call receives."""
    return {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "HOME": home if home is not None else os.environ.get("HOME", "/")}


def run(git_path, cwd, *args, timeout, allow=(0,), hooks_path=None):
    """Run ``git -C cwd`` and return its CompletedProcess.

    Raises ``GitCommandError`` when the process cannot start, times out, or
    exits with a code outside ``allow``. The caller names its own error type.
    """
    command = [git_path, "-C", str(cwd)]
    if hooks_path is not None:
        command.extend(("-c", "core.hooksPath={}".format(hooks_path)))
    try:
        result = subprocess.run([*command, *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout,
                                env=environment(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitCommandError(args[0] if args else "git", None, str(exc)) from exc
    if result.returncode not in allow:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitCommandError(args[0] if args else "git", result.returncode, detail)
    return result


@contextmanager
def exclusive_lock(path, timeout, busy_message):
    """Hold an advisory exclusive lock on ``path`` or fail after ``timeout``."""
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
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
                    raise LockTimeout(busy_message)
                time.sleep(0.05)
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - exercised on Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
