#!/usr/bin/env python3
"""Isolated exact-commit staging for the safe update (#119, #155).

``inspect_checkout`` accepts only a positively identified, ordinary,
user-owned, clean checkout of the canonical HTTPS source repository.
``stage_release`` creates a brand-new private repository in the state
directory, cloned only from the fixed canonical HTTPS remote at the exact
approved release commit, detached, with hooks and checkout filters
disabled, and verifies the fetched object and the complete manifest
inventory before the stage receipt is committed. Nothing staged is ever
executed here: not the staged tests, not release-provided hook scripts,
and not ``scripts/update.sh`` — the legacy pull/restart updater stays a
separate manual operation on the active checkout, never a stage action.

Every git call uses an absolute trusted executable (resolved the way the
relay resolves its own git: AGENT_RELAY_GIT, else the system paths, never
PATH) and an environment built from nothing: no variable of this process
reaches git, system and global configuration are switched off, prompts
are impossible and only the https protocol is allowed. A repository
configuration that can make git execute code is inspected and refused
before any command that could run a checkout filter is issued.
"""

import base64
import hashlib
import re
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from stat import S_ISDIR, S_ISREG, S_IMODE

import release_identity
from release_identity import UpdateError

COMMIT_RE = re.compile(r"[0-9a-f]{40}$")

STAGE_SCHEMA = "agent-relay.stage/v1"
REPOSITORY = release_identity.REPOSITORY
# The only origin a source checkout may have, and the only remote a stage is
# ever fetched from: plain anonymous HTTPS, so no ssh, no git://, no helper,
# no redirect and no credential prompt is possible.
SINGLE_ORIGIN = "https://github.com/%s" % REPOSITORY
ALLOWED_ORIGINS = frozenset((SINGLE_ORIGIN, SINGLE_ORIGIN + ".git"))
# The trusted git locations; a git found through PATH could be planted by
# anyone who can write there, so PATH is never searched.
TRUSTED_GIT_PATHS = ("/usr/bin/git", "/bin/git")
GIT_TIMEOUT_S = 60
# Prefixes that describe a system-managed or otherwise not-user location; a
# checkout there is coordinated by the OS or packaging, not by the operator,
# and is refused rather than guessed at.
SYSTEM_PREFIXES = ("/", "/usr", "/usr/local", "/bin", "/sbin", "/lib", "/lib64",
                   "/etc", "/opt", "/var", "/boot", "/System", "/Library",
                   "/private", "/Applications")
PACKAGE_PATH_PARTS = frozenset(("site-packages", "dist-packages", "egg-info"))
# Tracked modes of an ordinary checked-out file. Any other tracked mode is a
# symlink (120000), a gitlink/submodule (160000) or otherwise unsafe.
TRACKED_MODES = frozenset(("100644", "100755"))
STAGE_RECEIPT_NAME = "stage-receipt.json"


class StagingError(UpdateError):
    """A checkout could not be identified or a release staged and verified."""


def _git_env():
    """An environment built from nothing: git reads no config but the repo's.

    System and global configuration are off, none of this process's variables
    reach git, and a credential prompt is impossible. HOME is kept so git can
    locate its default exec paths on macOS.
    """
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "GIT_ALLOW_PROTOCOL": "https", "LC_ALL": "C", "LANG": "C"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


def _user():
    owner = getattr(os, "geteuid", None)
    return owner() if owner is not None else None


def default_git():
    """The absolute trusted git: AGENT_RELAY_GIT if named, else system paths."""
    override = os.environ.get("AGENT_RELAY_GIT")
    if override:
        if not (os.path.isabs(override) and os.path.isfile(override)
                and os.access(override, os.X_OK)):
            raise StagingError("AGENT_RELAY_GIT must be an absolute path to an executable file")
        return override
    for path in TRUSTED_GIT_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise StagingError("no trusted git executable found; set AGENT_RELAY_GIT to an absolute path")


class Git:
    """The trusted git executable, run with the scrubbed environment always.

    Every invocation carries the hook kill switches as -c options, so no hook
    of any repository configuration can run, and a command that could execute
    a checkout filter is additionally gated by ``config_guard``, which inspects
    the configuration before the command is issued.
    """

    def __init__(self, exe):
        if not isinstance(exe, str) or not os.path.isabs(exe) \
                or not os.path.isfile(exe) or not os.access(exe, os.X_OK):
            raise StagingError("git must be an absolute path to an executable file")
        self.exe = exe

    def run(self, *args, cwd=None, local_file=False):
        # ``local_file`` widens the protocol allowlist for exactly one command:
        # an object fetch between two user-owned private repositories of this
        # same installation. Nothing else ever leaves https.
        extra = ("-c", "protocol.file.allow=always") if local_file else ()
        env = _git_env()
        if local_file:
            env = {**env, "GIT_ALLOW_PROTOCOL": "https:file"}
        try:
            process = subprocess.run(
                [self.exe, "--no-optional-locks", "-c", "core.hooksPath=%s" % os.devnull,
                 "-c", "core.autocrlf=false", "-c", "core.eol=lf", *extra, *args],
                env=env, cwd=None if cwd is None else str(cwd),
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise StagingError("git %s timed out after %ds; refusing to wait"
                               % (args[0] if args else "?", GIT_TIMEOUT_S)) from exc
        except OSError as exc:
            raise StagingError("the trusted git could not be run: %s" % exc) from exc
        if process.returncode != 0:
            detail = process.stderr.strip().replace("\n", "; ")[:400]
            raise StagingError("git %s failed: %s" % (args[0] if args else "?",
                                                      detail or "no error output"))
        return process.stdout

    def config_guard(self, cwd):
        """Read the repository config BEFORE any command that could execute a
        checkout filter, and refuse the definitions that can redirect git to
        code: filters, custom hook paths, config includes, per-URL rewrites and
        submodule definitions."""
        offending = set()
        for line in self.run("config", "--local", "--list", cwd=cwd).splitlines():
            key = line.split("=", 1)[0].lower()
            if key.split(".", 1)[0] in ("filter", "include", "includeif", "submodule", "url") \
                    or key == "core.hookspath":
                offending.add(key)
        if offending:
            raise StagingError("repository configuration defines %s; refusing git "
                               "that may execute code" % ", ".join(sorted(offending)))


def _plain_private_dir(root, label):
    """A real, user-owned directory: not a symlink, with no symlinked ancestor."""
    root = Path(root)
    try:
        info = os.lstat(str(root))
    except OSError as exc:
        raise StagingError("%s path does not exist or is unreachable; name the directory "
                           "the change describes" % label) from exc
    if not S_ISDIR(info.st_mode):
        raise StagingError("%s path is not a directory" % label)
    if os.path.islink(str(root)):
        raise StagingError("%s path is a symlink; refusing unsafe location" % label)
    resolved = root.resolve()
    if os.path.realpath(str(resolved)) != str(resolved):
        raise StagingError("%s path resolves elsewhere; refusing unsafe location" % label)
    for parent in resolved.parents:
        if os.path.islink(str(parent)):
            raise StagingError("%s path has a symlinked ancestor (%s)" % (label, parent))
    owner = _user()
    if owner is not None and info.st_uid != owner:
        raise StagingError("%s directory is not owned by this user (uid %d != %d); refusing "
                           "an installation this account did not create"
                           % (label, info.st_uid, owner))
    if info.st_mode & 0o022:
        raise StagingError("%s directory is group- or world-writable; refusing an unsafe "
                           "location" % label)
    return resolved


def inspect_checkout(root, *, git):
    """Identify one operator source checkout: ordinary, user-owned, clean, canonical.

    Returns ``{"root", "commit", "origin"}``. Every refusal names a documented
    reason for an unsupported configuration, and not one byte of the checkout
    (its HEAD, its config, its state) is written, changed or executed here.
    """
    git = git if isinstance(git, Git) else Git(git)
    resolved = _plain_private_dir(root, "checkout")
    resolved_str = str(resolved)
    for prefix in SYSTEM_PREFIXES:
        if resolved_str == prefix or resolved_str.startswith(prefix + os.sep):
            raise StagingError("checkout %s is under a system prefix (%s); refusing a "
                               "package-managed or root installation" % (resolved_str, prefix))
    parts = {part.lower() for part in resolved.parts}
    if parts & PACKAGE_PATH_PARTS:
        raise StagingError("checkout sits in an installed package location; refusing a "
                           "package-managed installation")
    if sys.prefix and resolved_str.startswith(sys.prefix.rstrip(os.sep) + os.sep):
        raise StagingError("checkout is inside the running Python's prefix (%s); refusing an "
                           "ambiguous environment location" % sys.prefix)
    dotgit = resolved / ".git"
    try:
        dot_info = os.lstat(str(dotgit))
    except OSError as exc:
        raise StagingError("checkout has no .git metadata; not a git checkout") from exc
    if not S_ISDIR(dot_info.st_mode):
        raise StagingError(".git is not a directory; a linked worktree or bare repository "
                           "pointer is refused")
    if git.run("rev-parse", "--is-bare-repository", cwd=resolved).strip() != "false":
        raise StagingError("this is a bare repository; refusing")
    head = git.run("rev-parse", "HEAD", cwd=resolved).strip()
    if len(head) != 40:
        raise StagingError("checkout HEAD is not a commit (an unborn branch?); refusing")
    main = git.run("worktree", "list", "--porcelain", cwd=resolved).split("\n", 1)[0]
    if main.startswith("worktree ") and main.split(" ", 1)[1].rstrip("/\\") != resolved_str:
        raise StagingError("git reports a linked worktree, not the main checkout; refusing")
    git.config_guard(cwd=resolved)
    for origin in git.run("config", "--local", "--get-all", "remote.origin.url",
                          cwd=resolved).splitlines():
        if origin not in ALLOWED_ORIGINS:
            raise StagingError("origin %s is not the canonical %s remote; refusing a "
                               "non-canonical source checkout" % (origin, REPOSITORY))
    for line in git.run("ls-files", "-s", "-z", cwd=resolved).split("\0"):
        if not line:
            continue
        mode, _stage, path = line.split(" ", 2)
        if mode not in TRACKED_MODES:
            label = {"120000": "symlink", "160000": "submodule/gitlink"}.get(mode, mode)
            raise StagingError("repository tracks %s at %s; refusing a checkout with %s "
                               "tracked content" % (mode, path, label))
        if path.startswith(("/", "~")) or ".." in Path(path).parts:
            raise StagingError("tracked path %s is not a plain repository path" % path)
    status = git.run("status", "--porcelain", "--untracked-files=all", cwd=resolved)
    if status.strip():
        raise StagingError("checkout has uncommitted or untracked content:\n%s"
                           % "\n".join(status.splitlines()[:8]))
    return {"root": resolved_str, "commit": head, "origin": SINGLE_ORIGIN}


def _walk_regular_files(resolved):
    """Every regular file and real directory below the stage, refusing symlinks
    and special files, modes that could carry setuid/setgid/sticky bits, and
    group/world writes. Entries of ``dirnames`` are classified by os.walk with
    the symlink followed (entry.is_dir() follows links), so a directory symlink
    is pruned here with its own lstat: without it, everything reachable through
    such a link would be invisible to the manifest comparison."""
    seen = {}
    walked_dirs = set()
    for dirpath, dirnames, filenames in os.walk(str(resolved), followlinks=False):
        current = Path(dirpath)
        if current != resolved:
            walked_dirs.add(current.relative_to(resolved).as_posix())
        if current == resolved and ".git" in dirnames:
            dirnames.remove(".git")  # .git is metadata, not manifest content
        for name in dirnames:
            info = os.lstat(str(current / name))
            if not S_ISDIR(info.st_mode):
                raise StagingError("staged %s is not a real directory; refusing symlinks "
                                   "and special files" % os.path.join(
                                       current.relative_to(resolved).as_posix(), name))
        for name in filenames:
            if name == STAGE_RECEIPT_NAME and current == resolved:
                continue  # the receipt is the stage's own identity record
            candidate = current / name
            relative = candidate.relative_to(resolved).as_posix()
            info = os.lstat(str(candidate))
            if not S_ISREG(info.st_mode):
                raise StagingError("staged %s is not a regular file; refusing symlinks and "
                                   "special files" % relative)
            if info.st_mode & (0o04000 | 0o02000 | 0o00020 | 0o00002):
                raise StagingError("staged %s has an unsafe file mode (%o)"
                                   % (relative, info.st_mode))
            seen[relative] = candidate
    return seen, walked_dirs


def _hash_file(path):
    digest = hashlib.sha256()
    try:
        with open(str(path), "rb") as handle:
            while True:
                chunk = handle.read(1 << 16)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise StagingError("cannot read the staged file %s: %s" % (path, exc)) from exc
    return digest.hexdigest()


def _receipt_manifest(resolved, identity):
    """Decode the manifest carried in the stage receipt and re-verify that its
    exact bytes are the manifest the release identity was built from."""
    path = resolved / STAGE_RECEIPT_NAME
    release_identity.private_file_checks(path, "stage receipt")
    try:
        receipt = release_identity.strict_json(path.read_bytes(), "stage receipt")
    except OSError as exc:
        raise StagingError("cannot read the stage receipt: %s" % exc) from exc
    if receipt.get("schema") != STAGE_SCHEMA:
        raise StagingError("stage receipt schema is not %s" % STAGE_SCHEMA)
    stored = receipt.get("identity")
    if not isinstance(stored, dict) \
            or release_identity.identity_digest(stored) != receipt.get("identityDigest"):
        raise StagingError("stage receipt digest does not match its stored identity")
    if release_identity.identity_digest(identity) != receipt.get("identityDigest"):
        raise StagingError("stage receipt was issued for a different release identity")
    try:
        raw = base64.b64decode(receipt.get("manifestB64", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise StagingError("stage receipt carries no manifest; refusing an unverified stage") from exc
    if hashlib.sha256(raw).hexdigest() != identity["manifestDigest"]:
        raise StagingError("the receipt's manifest is not the manifest of this release "
                           "identity; refusing")
    manifest = release_identity.strict_json(raw, "receipt manifest")
    return manifest


def verify_stage(stage, identity, *, git=None, manifest=None):
    """Verify one staged tree against the approved release identity.

    With ``manifest`` given (the stage-time call), the staged tree is checked
    against that exact manifest inventory: every path, mode and sha256 content
    hash, no extra or missing file, no symlink or special file, and a detached
    HEAD at the approved commit. Without it, the manifest bound into the stage
    receipt is re-verified against the identity first. Refusals change nothing.
    """
    git = git if (isinstance(git, Git) or git is None) else Git(git)
    resolved = _plain_private_dir(stage, "stage")
    dotgit = resolved / ".git"
    if not os.path.isdir(str(dotgit)) or os.path.islink(str(dotgit)):
        raise StagingError("stage has no private git directory; refusing an unverified stage")
    if git is None:
        git = Git(default_git())
    head = git.run("rev-parse", "HEAD", cwd=resolved).strip()
    symbolic = git.run("branch", "--show-current", cwd=resolved).strip()
    if head != identity["commit"]:
        raise StagingError("stage HEAD %s is not the approved commit %s"
                           % (head, identity["commit"]))
    if symbolic:
        raise StagingError("stage HEAD is on branch %s; a stage must be detached" % symbolic)
    git.config_guard(cwd=resolved)
    if manifest is None:
        manifest = _receipt_manifest(resolved, identity)
    release_identity.validate_manifest(manifest, identity)
    inventory = sorted((entry["path"], entry["mode"], entry["sha256"])
                       for entry in manifest["files"])
    staged, staged_dirs = _walk_regular_files(resolved)
    manifest_paths = {entry[0] for entry in inventory}
    manifest_dirs = set()
    for path in manifest_paths:
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            manifest_dirs.add(parent.as_posix())
            parent = parent.parent
    extra_dirs = sorted(staged_dirs - manifest_dirs)
    if extra_dirs:
        raise StagingError("stage contains directories the manifest cannot describe: %s"
                           % ", ".join(extra_dirs[:8]))
    extra = sorted(set(staged) - manifest_paths)
    missing = sorted(manifest_paths - set(staged))
    if extra:
        raise StagingError("stage contains extra files not in the manifest: %s"
                           % ", ".join(extra[:8]))
    if missing:
        raise StagingError("stage is missing manifest files: %s" % ", ".join(missing[:8]))
    for path, mode, digest in inventory:
        target = staged[path]
        actual = "100755" if S_IMODE(os.lstat(str(target)).st_mode) & 0o111 else "100644"
        if actual != mode:
            raise StagingError("staged %s mode %s does not match the manifest mode %s"
                               % (path, actual, mode))
        if _hash_file(target) != digest:
            raise StagingError("staged %s content does not match the manifest sha256" % path)
    if git.run("status", "--porcelain", "--untracked-files=no", cwd=resolved).strip():
        raise StagingError("the staged tree differs from its index; refusing a mixed stage")
    return [{"path": path, "mode": mode, "sha256": digest}
            for path, mode, digest in inventory]


def _staging_root(state_dir):
    state = release_identity.private_state_dir(Path(state_dir), "staging state")
    staging = state / "staging"
    staging.mkdir(mode=0o700, exist_ok=True)
    release_identity.private_state_dir(staging, "staging state")
    return staging


def stage_release(identity, *, root, state_dir, git, manifest, manifest_bytes, now=None):
    """Stage one exact approved release commit in a brand-new private repository.

    The approval consent recorded for this exact identity is consumed first (it
    must have been recorded on this device for this installation). The stage is
    cloned only from the canonical HTTPS remote, checked out detached with hooks
    disabled, and its complete manifest inventory is verified from the written
    files before the receipt (which binds the full identity, the exact original
    commit and root, and the verified inventory) is committed. Nothing from the
    stage is ever executed by this flow, and the active checkout is never
    written: on success or failure its HEAD, config and state stay intact.
    """
    git = git if isinstance(git, Git) else Git(git)
    now = time.time() if now is None else now
    if hashlib.sha256(manifest_bytes).hexdigest() != identity["manifestDigest"]:
        raise StagingError("the resolved manifest bytes do not match the release identity")
    release_identity.validate_manifest(manifest, identity)
    if str(Path(root).resolve()) != str(release_identity.installation_path()):
        raise StagingError("the approved consent is bound to installation %s; refusing to "
                           "stage for a different install-dir"
                           % release_identity.installation_path())
    inspected = inspect_checkout(root, git=git)
    release_identity.consume_consent(identity, state_dir=state_dir, now=now)
    staging_root = _staging_root(state_dir)
    # mkdtemp creates a brand-new private (0700) directory; no existing stage is
    # ever reused, and a failed stage is removed rather than left unusable.
    stage_dir = Path(tempfile.mkdtemp(prefix="stage-%s-" % identity["tag"],
                                      dir=str(staging_root)))
    try:
        git.run("clone", "--no-checkout", "--no-hardlinks", SINGLE_ORIGIN + ".git",
                str(stage_dir), cwd=str(staging_root))
        try:
            object_kind = git.run("cat-file", "-t", identity["commit"], cwd=stage_dir).strip()
        except StagingError:
            # the commit may live only under the published tag; a fetch runs no
            # checkout, so no hook or filter can execute for it
            git.run("fetch", "origin", "--no-recurse-submodules",
                    "+refs/tags/%s:refs/tags/%s" % (identity["tag"], identity["tag"]),
                    cwd=stage_dir)
            object_kind = git.run("cat-file", "-t", identity["commit"], cwd=stage_dir).strip()
        if object_kind != "commit":
            raise StagingError("the approved object %s is a %s, not a commit; refusing to "
                               "stage" % (identity["commit"], object_kind))
        git.config_guard(cwd=stage_dir)
        git.run("checkout", "--detach", identity["commit"], cwd=stage_dir)
        verify_stage(stage_dir, identity, git=git, manifest=manifest)
        # re-resolve the canonical identity one last time before committing the receipt
        if git.run("rev-parse", "HEAD", cwd=stage_dir).strip() != identity["commit"]:
            raise StagingError("the stage moved between verification and receipt; refusing")
        receipt = {
            "schema": STAGE_SCHEMA,
            "identity": identity,
            "identityDigest": release_identity.identity_digest(identity),
            "manifestB64": base64.b64encode(manifest_bytes).decode("ascii"),
            "sourceRoot": inspected["root"],
            "sourceCommit": inspected["commit"],
            "stage": str(stage_dir),
            "git": git.exe,
            "createdAt": now,
        }
        release_identity._atomic_write(stage_dir / STAGE_RECEIPT_NAME,
                                       json.dumps(receipt, indent=1).encode("utf-8"))
    except BaseException:
        shutil.rmtree(str(stage_dir), ignore_errors=True)
        raise
    return receipt


def activate_transaction_source(record, *, git=None):
    """Swap the verified checkout to the staged exact commit, once, journaled elsewhere.

    ``record`` is the live activation transaction: the checkout it names is the
    previously verified ordinary one, and the target is the verified stage's
    commit. The caller holds the transaction/install lock and has just
    journaled every pre-effect fact; this function performs the one source
    effect: while the original HEAD, cleanliness and canonical origin recheck,
    the target commit's object is copied from the stage's own repository into
    the checkout (hooks and filters are off; the local fetch runs only between
    two user-owned private repositories of this same installation), the
    known-good original commit is kept both in the checkout's history and as an
    explicit rollback ref, and only then is the working tree switched. The
    switched tree is re-verified (detached HEAD at the target, clean status,
    ordinary tracked modes, the previous object still present) before it is
    accepted, so a failed or partial switch raises instead of advancing.

    The original service definitions, PATH, port and configuration are outside
    this function: it writes only git metadata inside the checkout itself.
    Returns ``{"target", "previousObject"}``.
    """
    git = git if isinstance(git, Git) else Git(git or default_git())
    record = record if isinstance(record, dict) else None
    if record is None:
        raise StagingError("activation needs the live transaction record")
    inventory = record.get("inventory") or {}
    root = inventory.get("sourceRoot")
    resolved = _plain_private_dir(root, "checkout")
    inspected = inspect_checkout(resolved, git=git)
    revision = record.get("revision") or ""
    target = (record.get("target") or {}).get("identity", {}).get("commit") or ""
    if not COMMIT_RE.fullmatch(target):
        raise StagingError("the transaction names no exact 40-hex target commit; refusing")
    if inspected["commit"] != revision or inspected["root"] != str(resolved):
        raise StagingError("the checkout at %s is %s at HEAD %s, not the transaction's "
                           "cleanly prepared original; refusing an unsafe activation"
                           % (resolved, inspected["root"], inspected["commit"]))
    try:
        stage = Path(record["stage"]["path"])
    except (KeyError, TypeError) as exc:
        raise StagingError("the transaction names no verified stage; refusing") from exc
    _plain_private_dir(stage, "stage")
    # keep the known-good original reachable as an explicit rollback ref, too
    git.run("update-ref", "refs/agent-relay/update-rollback/%s" % record["transactionId"],
            revision, cwd=resolved)
    # a local object transport between two private user-owned repositories:
    # hooks and filters are dead for the fetch and the checkout that follows
    git.run("fetch", "--no-recurse-submodules", str(stage), target, cwd=resolved,
            local_file=True)
    git.config_guard(cwd=resolved)
    git.run("checkout", "--detach", target, cwd=resolved)
    if git.run("rev-parse", "HEAD", cwd=resolved).strip() != target:
        raise StagingError("the checkout HEAD moved while switching to the target "
                           "commit; refusing")
    if git.run("status", "--porcelain", "--untracked-files=all", cwd=resolved).strip():
        raise StagingError("the switched working tree is dirty; refusing an uncertain "
                           "activation")
    for line in git.run("ls-files", "-s", "-z", cwd=resolved).split("\0"):
        if not line:
            continue
        mode, _stage, path = line.split(" ", 2)
        if mode not in TRACKED_MODES:
            raise StagingError("activated repository tracks unsafe mode %s at %s; "
                               "refusing" % (mode, path))
    if git.run("cat-file", "-t", revision, cwd=resolved).strip() != "commit":
        raise StagingError("the previous commit object %s is no longer available; only a "
                           "known-good previous revision may be activated from" % revision)
    return {"target": target, "previousObject": revision}


def rollback_transaction_source(record, *, git=None):
    """Restore the verified checkout to the transaction's exact prior revision.

    ``record`` is the live activation transaction; the caller holds the
    transaction/install lock. The exact prior commit comes only from the
    durable journal — never from a branch, main or a release tag — and it is
    proven still retained as a commit object in the checkout's own repository
    before anything is restored. The restoration runs only on a checkout
    ``inspect_checkout`` still accepts: any intervening user edit, symlink,
    foreign origin or unsafe path mutation is refused, never overwritten.
    A checkout that already sits at the prior revision (the failure happened
    before the swap) is proven, not touched again. The restored tree is
    re-verified (detached HEAD at the prior commit, clean status, ordinary
    tracked modes) before it is accepted, so a failed or partial restore
    raises instead of pretending to be finished. Only git metadata inside the
    checkout is written; configuration, service definitions and state are
    outside this function.

    Returns ``{"previous", "root", "restored", "replacedCommit"}``.
    """
    git = git if isinstance(git, Git) else Git(git or default_git())
    record = record if isinstance(record, dict) else None
    if record is None:
        raise StagingError("rollback needs the live transaction record")
    inventory = record.get("inventory") or {}
    root = inventory.get("sourceRoot")
    resolved = _plain_private_dir(root, "checkout")
    revision = record.get("revision") or ""
    if not COMMIT_RE.fullmatch(revision):
        raise StagingError("the transaction names no exact 40-hex prior revision; "
                           "refusing to restore anything but the journaled known-good revision")
    replaced = None
    try:
        replaced = (record["target"] or {}).get("identity", {}).get("commit") or None
    except (KeyError, TypeError, AttributeError):
        replaced = None
    # refuses a dirty tree, a symlinked or foreign checkout, uncommitted or
    # untracked content: an intervening user edit is never overwritten
    inspected = inspect_checkout(resolved, git=git)
    kind = git.run("cat-file", "-t", revision, cwd=resolved).strip()
    if kind != "commit":
        raise StagingError("the journaled prior revision %s is not retained as a commit "
                           "object in %s; refusing to restore anything but the exact "
                           "known-good revision" % (revision, resolved))
    if inspected["commit"] == revision:
        return {"previous": revision, "root": str(resolved), "restored": False,
                "replacedCommit": replaced}
    git.run("checkout", "--detach", revision, cwd=resolved)
    if git.run("rev-parse", "HEAD", cwd=resolved).strip() != revision:
        raise StagingError("the checkout HEAD moved while restoring the prior revision; "
                           "refusing an uncertain rollback")
    if git.run("status", "--porcelain", "--untracked-files=all", cwd=resolved).strip():
        raise StagingError("the restored working tree is dirty; refusing an uncertain rollback")
    for line in git.run("ls-files", "-s", "-z", cwd=resolved).split("\0"):
        if not line:
            continue
        mode, _stage, path = line.split(" ", 2)
        if mode not in TRACKED_MODES:
            raise StagingError("the restored repository tracks unsafe mode %s at %s; "
                               "refusing" % (mode, path))
    return {"previous": revision, "root": str(resolved), "restored": True,
            "replacedCommit": replaced}
