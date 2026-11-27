"""Export selected team-run records to a dedicated, local Git repository."""
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import team_git


class TeamHistoryError(RuntimeError):
    """A team history export could not be completed."""


_GIT_TIMEOUT = 15
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _git(git_path, repo, *args, allow=(0,)):
    if team_git.executable(git_path) is None:
        raise TeamHistoryError("git_path must be an absolute path to an executable git")
    try:
        result = team_git.run(git_path, repo, *args, timeout=_GIT_TIMEOUT, allow=allow)
    except team_git.GitCommandError as exc:
        if exc.code is None:
            raise TeamHistoryError("git history operation failed: {}".format(exc.detail)) from exc
        raise TeamHistoryError("git {} failed ({}): {}".format(exc.command, exc.code, exc.detail)) from exc
    return result.stdout.decode("utf-8", "strict").strip()


@contextmanager
def _repo_lock(repo):
    lock = repo.parent / (repo.name + ".export-lock")
    try:
        with team_git.exclusive_lock(lock, _GIT_TIMEOUT,
                                     "timed out waiting for history export lock: {}".format(lock)):
            yield
    except team_git.LockTimeout as exc:
        raise TeamHistoryError(str(exc)) from exc


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
    artifacts = [{"id": a["id"], "actor": a["actor"], "kind": a["kind"], "title": a["title"],
                  "uri": a["uri"], "created": a["created"]}
                 for a in run.get("artifacts", [])]
    if artifacts:
        files[root + "artifacts.json"] = (json.dumps(artifacts, ensure_ascii=False, sort_keys=True,
                                                         indent=2, allow_nan=False) + "\n").encode("utf-8")
    return files


def export_run_history(store, run_id, history_repo, git_path):
    """Export specification, decisions, artifacts and closure milestones; return SHA and paths.

    ``history_repo`` is the service's dedicated state location, separate from source
    checkouts. Operations are local Git init/add/commit only; this never pushes.
    """
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise TeamHistoryError("invalid team run id")
    if not isinstance(git_path, str) or not os.path.isabs(git_path) or not os.path.isfile(git_path) or not os.access(git_path, os.X_OK):
        raise TeamHistoryError("git_path must be an absolute path to an executable git")
    if not isinstance(history_repo, (str, os.PathLike)) or not str(history_repo).strip():
        raise TeamHistoryError("history_repo must be a nonempty path")
    requested = Path(history_repo).expanduser()
    if not requested.is_absolute():
        raise TeamHistoryError("history_repo must be an absolute path")
    if requested.is_symlink():
        raise TeamHistoryError("history_repo must not be a symlink")
    run = store.get_run(run_id)
    files = _records(run)
    repo = Path(os.path.realpath(requested))
    repo.parent.mkdir(parents=True, exist_ok=True)
    with _repo_lock(repo):
        repo.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (repo / ".git").exists():
            _git(git_path, repo, "init", "--quiet")
        if _git(git_path, repo, "remote"):
            raise TeamHistoryError("history repository has configured remotes; refusing export")
        os.chmod(repo, 0o700)
        for relative, content in files.items():
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                raise TeamHistoryError("history export destination is a symlink: {}".format(relative))
            if not destination.parent.resolve().is_relative_to(repo):
                raise TeamHistoryError("history export path escapes the repository: {}".format(relative))
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
        prefix = "runs/{}/".format(run_id)
        # Refuse any staged content this export will not write. Committed files
        # under this run may legitimately be staged for deletion by a retry;
        # a hand-staged or foreign file is not generated content.
        try:
            head = team_git.run(git_path, repo, "rev-parse", "--verify", "--quiet", "HEAD",
                                timeout=_GIT_TIMEOUT, allow=(0, 1))
        except team_git.GitCommandError as exc:
            raise TeamHistoryError("git HEAD check failed: {}".format(exc.detail)) from exc
        committed = set()
        if head.returncode == 0:
            committed = set(_git(git_path, repo, "ls-tree", "-r", "--name-only", "HEAD", "--", prefix).splitlines())
        generated = set(paths)
        allowed = generated | (committed - generated)
        try:
            staged = team_git.run(git_path, repo, "diff", "--cached", "--name-only", "-z",
                                  timeout=_GIT_TIMEOUT)
        except team_git.GitCommandError as exc:
            raise TeamHistoryError("git staged path check failed: {}".format(exc.detail)) from exc
        staged_paths = staged.stdout.decode("utf-8", "surrogateescape").split("\0")
        unexpected = [path for path in staged_paths if path and path not in allowed]
        if unexpected:
            raise TeamHistoryError("history repository has staged files outside this export: {}".format(", ".join(unexpected)))
        if paths:
            _git(git_path, repo, "add", "--", *paths)
        prior = _git(git_path, repo, "ls-files", "--", prefix).splitlines()
        obsolete = sorted(set(prior) - set(paths))
        if obsolete:
            _git(git_path, repo, "rm", "--quiet", "--", *obsolete)
        try:
            diff = team_git.run(git_path, repo, "diff", "--cached", "--quiet",
                                timeout=_GIT_TIMEOUT, allow=(0, 1))
        except team_git.GitCommandError as exc:
            raise TeamHistoryError("git staged diff failed: {}".format(exc.detail)) from exc
        changed = diff.returncode == 1
        if changed:
            _git(git_path, repo, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                 "-c", "user.name=Agent Relay", "-c", "user.email=agent-relay@localhost",
                 "commit", "--quiet", "-m", "Export team run {}".format(run_id))
        head = _git(git_path, repo, "rev-parse", "HEAD")
        return {"commit": head, "paths": paths, "changed": changed}
