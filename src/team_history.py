"""Export selected team-run records to a dedicated, local Git repository."""
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt


class TeamHistoryError(RuntimeError):
    """A team history export could not be completed."""


_GIT_TIMEOUT = 15
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _env():
    return {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.environ.get("HOME", "/")}


def _git(git_path, repo, *args):
    if not os.path.isabs(git_path) or not os.path.isfile(git_path) or not os.access(git_path, os.X_OK):
        raise TeamHistoryError("git_path must be an absolute path to an executable git")
    try:
        result = subprocess.run([git_path, "-C", str(repo), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=_GIT_TIMEOUT, env=_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TeamHistoryError("git history operation failed: {}".format(exc)) from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise TeamHistoryError("git {} failed ({}): {}".format(args[0], result.returncode, detail))
    return result.stdout.decode("utf-8", "strict").strip()


@contextmanager
def _repo_lock(repo):
    lock = repo.parent / (repo.name + ".export-lock")
    deadline = time.monotonic() + _GIT_TIMEOUT
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TeamHistoryError("timed out waiting for history export lock: {}".format(lock))
                time.sleep(0.05)
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - exercised on Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _decision(event):
    try:
        body = json.loads(event["body"])
    except (TypeError, ValueError) as exc:
        raise TeamHistoryError("stored conclusion {} is not valid JSON".format(event["id"])) from exc
    if not isinstance(body, dict) or not isinstance(body.get("summary"), str) or not isinstance(body.get("rationale"), str):
        raise TeamHistoryError("stored conclusion {} has invalid decision fields".format(event["id"]))
    return {"id": event["id"], "thread_id": event.get("thread_id"),
            "author": event["actor"], "created": event["created"],
            "summary": body["summary"], "rationale": body["rationale"],
            "action": body.get("action"), "needs_lead": bool(body.get("needs_lead", False))}


def _records(run):
    """Project only agreed artifacts and selected provenance; never message bodies."""
    root = "runs/{}/".format(run["id"])
    # A stable index also makes an early run export useful before it has a spec.
    index = {key: run[key] for key in ("id", "objective", "lead", "phase", "approved", "closed", "created")}
    files = {root + "run.json": (json.dumps(index, ensure_ascii=False, sort_keys=True,
                                               indent=2, allow_nan=False) + "\n").encode("utf-8")}
    if run.get("spec"):
        title = "Approved specification" if run.get("approved") else "Draft specification"
        files[root + "specification.md"] = (
            "# {}\n\nRun: `{}`\n\nLead: `{}`\n\nApproved: `{}`\n\n"
            "---\n\n{}\n".format(title, run["id"], run["lead"], str(bool(run["approved"])).lower(), run["spec"])
        ).encode("utf-8")
    decisions = [_decision(e) for e in run.get("events", []) if e.get("kind") == "conclusion"]
    if decisions:
        files[root + "decisions.json"] = (json.dumps(decisions, ensure_ascii=False, sort_keys=True,
                                                         indent=2, allow_nan=False) + "\n").encode("utf-8")
    milestones = [{"id": e["id"], "author": e["actor"], "created": e["created"], "summary": e["body"]}
                  for e in run.get("events", []) if e.get("kind") == "closed"]
    if milestones:
        files[root + "milestones.json"] = (json.dumps(milestones, ensure_ascii=False, sort_keys=True,
                                                         indent=2, allow_nan=False) + "\n").encode("utf-8")
    return files


def export_run_history(store, run_id, history_repo, git_path):
    """Export specification, decisions and closure milestones; return SHA and paths.

    ``history_repo`` is the service's dedicated state location, separate from source
    checkouts. Operations are local Git init/add/commit only; this never pushes.
    """
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise TeamHistoryError("invalid team run id")
    if not isinstance(git_path, str) or not os.path.isabs(git_path) or not os.path.isfile(git_path) or not os.access(git_path, os.X_OK):
        raise TeamHistoryError("git_path must be an absolute path to an executable git")
    run = store.get_run(run_id)
    files = _records(run)
    repo = Path(history_repo).expanduser().absolute()
    repo.parent.mkdir(parents=True, exist_ok=True)
    with _repo_lock(repo):
        repo.mkdir(parents=True, exist_ok=True)
        os.chmod(repo, 0o700)
        if not (repo / ".git").exists():
            _git(git_path, repo, "init", "--quiet")
        if _git(git_path, repo, "remote"):
            raise TeamHistoryError("history repository has configured remotes; refusing export")
        for relative, content in files.items():
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() == content:
                continue
            fd, temp_name = tempfile.mkstemp(prefix=".history-", dir=str(destination.parent))
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp_name, destination)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        paths = sorted(files)
        if paths:
            _git(git_path, repo, "add", "--", *paths)
        prior = _git(git_path, repo, "ls-files", "--", "runs/{}/".format(run_id)).splitlines()
        obsolete = sorted(set(prior) - set(paths))
        if obsolete:
            _git(git_path, repo, "rm", "--quiet", "--", *obsolete)
        # Never accidentally commit another run's pre-staged work.
        try:
            staged = subprocess.run([git_path, "-C", str(repo), "diff", "--cached", "--name-only", "-z"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_GIT_TIMEOUT,
                                    env=_env(), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamHistoryError("git staged path check failed: {}".format(exc)) from exc
        if staged.returncode:
            raise TeamHistoryError("git staged path check failed: {}".format(staged.stderr.decode("utf-8", "replace").strip()))
        prefix = "runs/{}/".format(run_id)
        staged_paths = staged.stdout.decode("utf-8", "surrogateescape").split("\0")
        unrelated = [path for path in staged_paths if path and not path.startswith(prefix)]
        if unrelated:
            raise TeamHistoryError("history repository has staged files outside this run: {}".format(", ".join(unrelated)))
        try:
            diff = subprocess.run([git_path, "-C", str(repo), "diff", "--cached", "--quiet"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  timeout=_GIT_TIMEOUT, env=_env(), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamHistoryError("git staged diff failed: {}".format(exc)) from exc
        if diff.returncode not in (0, 1):
            raise TeamHistoryError("git staged diff failed: {}".format(diff.stderr.decode("utf-8", "replace").strip()))
        changed = diff.returncode == 1
        if changed:
            _git(git_path, repo, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                 "-c", "user.name=Agent Relay", "-c", "user.email=agent-relay@localhost",
                 "commit", "--quiet", "-m", "Export team run {}".format(run_id))
        head = _git(git_path, repo, "rev-parse", "HEAD")
        return {"commit": head, "paths": paths, "changed": changed}
