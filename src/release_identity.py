#!/usr/bin/env python3
"""Resolve, validate and consent to a published agent-relay release identity.

The canonical repository is exactly ``Ami-Khokhar/agent-relay`` and only stable
published GitHub HTTPS releases count. ``target_commitish`` is never accepted as
proof that a tag points at a commit: tags are resolved through the git
refs/tags API, and an annotated tag is peeled once through the git/tags API
until an immutable 40-hex-character commit is reached.
"""

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import release_discovery

try:
    import fcntl
except ImportError:
    fcntl = None

REPOSITORY = "Ami-Khokhar/agent-relay"
API_BASE = "https://api.github.com/repos/" + REPOSITORY
RELEASE_BY_TAG_URL = API_BASE + "/releases/tags/"
TAG_REF_URL = API_BASE + "/git/ref/tags/"
TAG_PEEK_URL = API_BASE + "/git/tags/"
ASSET_URL = API_BASE + "/releases/assets/"
MANIFEST_NAME = "agent-relay-manifest.json"
MANIFEST_SCHEMA = "agent-relay.update/v1"
CONSENT_SCHEMA = "agent-relay.consent/v1"
MAX_CONSENT_BYTES = 64 * 1024
MAX_MANIFEST = 512 * 1024
MAX_MANIFEST_FILES = 5000
CONSENT_MAX_AGE = 15 * 60
VERSION_PROMPT = "Type the exact release version to approve: "
COMMIT_PROMPT = "Type the exact 40-character commit to approve: "
CONSENT_PROMPT = "Type the word approve (this records consent only; nothing is installed): "
IS_POSIX = os.name == "posix"
HEX_RE = re.compile(r"^([0-9a-f]{40})$")
SHA256_RE = re.compile(r"^([0-9a-f]{64})$")
DEVICE_RE = re.compile(r"^([0-9a-f]{32})$")
NONCE_RE = re.compile(r"^([0-9a-f]{32,128})$")
ALLOWED_MODES = ("100644", "100755")


class UpdateError(Exception):
    """A release could not be resolved, validated or consented to."""


class _NoRedirect(HTTPRedirectHandler):
    """Disallow every redirect so identity data stays on the canonical host."""
    def http_error_301(self, req, fp, code, msg, headers):
        raise UpdateError("release service redirected; refusing non-canonical identity")

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def canonical_url(kind, value):
    """Map a bounded identity request kind to its exact canonical HTTPS URL."""
    bases = {"release": RELEASE_BY_TAG_URL, "ref": TAG_REF_URL,
             "peel": TAG_PEEK_URL, "asset": ASSET_URL}
    if kind not in bases:
        raise UpdateError("unknown release identity request kind")
    return bases[kind] + value


def no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise UpdateError("release metadata contains a duplicate JSON key")
    return dict(pairs)


def strict_json(raw, description):
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("%s is not valid JSON" % description) from exc
    if not isinstance(data, dict):
        raise UpdateError("%s is not a JSON object" % description)
    return data


def limited_fetch(fetch_json, url):
    """Call the injected fetch and enforce the response size bound."""
    data = fetch_json(url)
    if not isinstance(data, (bytes, bytearray)):
        raise UpdateError("release metadata fetch returned non-bytes results")
    data = bytes(data)
    if len(data) > release_discovery.MAX_RESPONSE:
        raise UpdateError("release metadata exceeded the response size limit")
    return data


def commit_sha(object_id):
    if not isinstance(object_id, str) or not HEX_RE.fullmatch(object_id):
        raise UpdateError("git object id is not a 40 hexadecimal character commit sha")
    return object_id


def peel_commit(ref, fetch_json):
    # A lightweight tag points at a commit object; an annotated tag points at a
    # tag object that peels once to a commit. `target_commitish` is not proof.
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
        raise UpdateError("tag reference is not a git ref object")
    object_id, object_type = ref["object"].get("sha"), ref["object"].get("type")
    if object_type == "commit":
        return commit_sha(object_id)
    if object_type != "tag":
        raise UpdateError("tag object is not a lightweight or annotated tag")
    peeled = strict_json(limited_fetch(fetch_json, canonical_url("peel", commit_sha(object_id))),
                         "annotated tag peel object")
    inner = peeled.get("object")
    if not isinstance(inner, dict) or inner.get("type") != "commit":
        raise UpdateError("annotated tag does not point at a commit object")
    return commit_sha(inner.get("sha"))


def stable_release(release, tag):
    """Reject every release that is not the exact canonical stable published one."""
    if not isinstance(release, dict):
        raise UpdateError("release metadata is not a JSON object")
    if release.get("tag_name") != tag:
        raise UpdateError("release metadata does not describe the requested tag")
    if release.get("draft") is True or release.get("prerelease") is True:
        raise UpdateError("release is a draft or prerelease; only stable releases count")
    if release.get("html_url") != release_discovery.RELEASE_URL + tag:
        raise UpdateError("release metadata is not the canonical published release")
    assets = release.get("assets")
    if not isinstance(assets, list) or not assets:
        raise UpdateError("release has no published assets")
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == MANIFEST_NAME:
            return asset
    raise UpdateError("release does not publish an %s asset" % MANIFEST_NAME)


def validate_file_entry(entry):
    if not isinstance(entry, dict):
        raise UpdateError("manifest file entry is not an object")
    path = entry.get("path")
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path \
            or path.startswith(("/", "~", "./")) or ".." in Path(path).parts \
            or any(ord(char) < 0x20 for char in path):
        raise UpdateError("manifest file path is not a plain tracked repository path")
    if entry.get("mode") not in ALLOWED_MODES:
        raise UpdateError("manifest file mode is not a tracked regular-file mode")
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise UpdateError("manifest file sha256 is not 64 lowercase hexadecimal characters")
    return {"path": path, "mode": entry["mode"], "sha256": digest}


def validate_manifest(manifest, identity):
    """Check that a parsed manifest binds exactly the resolved release identity."""
    if not isinstance(manifest, dict):
        raise UpdateError("release manifest is not a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise UpdateError("release manifest schema is not %s" % MANIFEST_SCHEMA)
    if manifest.get("repository") != identity["repository"]:
        raise UpdateError("release manifest repository does not match the release identity")
    if manifest.get("version") != identity["tag"] or manifest.get("tag") != identity["tag"]:
        raise UpdateError("release manifest version or tag does not match the release tag")
    if manifest.get("commit") != identity["commit"]:
        raise UpdateError("release manifest commit does not match the peeled release commit")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_MANIFEST_FILES:
        raise UpdateError("release manifest file inventory is empty or too large")
    manifest["files"] = [validate_file_entry(entry) for entry in files]
    return manifest


def identity_digest(identity):
    """Stable digest over the full release identity that consent binds."""
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_release(tag, *, fetch_json):
    """Resolve one stable tag to an exact immutable release identity.

    Returns ``(identity, manifest, manifest_bytes)``. The identity carries the
    repository, release id, tag, peeled 40-hex commit, manifest asset id and
    sha256 digest, and the release/asset identity timestamps. No release is a
    clear refusal raised as :class:`UpdateError`, never an invented identity.
    """
    if not isinstance(tag, str):
        raise UpdateError("release tag must be a string")
    try:
        version = release_discovery._version(tag)
    except (ValueError, TypeError) as exc:
        raise UpdateError("release tag is not semantic versioning") from exc
    if version[3]:
        raise UpdateError("release tag is a prerelease; only stable releases count")
    if len(tag) > release_discovery.MAX_VERSION:
        raise UpdateError("release tag is too long")
    release = strict_json(limited_fetch(fetch_json, canonical_url("release", tag)),
                          "release metadata")
    asset = stable_release(release, tag)
    asset_id = asset.get("id")
    if not isinstance(asset_id, int) or asset_id <= 0:
        raise UpdateError("manifest asset is not a stable published GitHub asset")
    ref = strict_json(limited_fetch(fetch_json, canonical_url("ref", tag)), "tag ref")
    commit = peel_commit(ref, fetch_json)
    manifest_bytes = limited_fetch(fetch_json, canonical_url("asset", str(asset_id)))
    if len(manifest_bytes) > MAX_MANIFEST:
        raise UpdateError("release manifest asset exceeded the content size limit")
    manifest = strict_json(manifest_bytes, "release manifest asset")
    identity = {
        "repository": REPOSITORY,
        "releaseId": release.get("id"),
        "tag": tag,
        "commit": commit,
        "assetId": asset_id,
        "manifestDigest": hashlib.sha256(manifest_bytes).hexdigest(),
        "publishedAt": release.get("published_at"),
        "assetUpdatedAt": asset.get("updated_at"),
    }
    if not isinstance(identity["releaseId"], int) or identity["releaseId"] <= 0:
        raise UpdateError("release id is not a stable published GitHub release")
    for stamp in ("publishedAt", "assetUpdatedAt"):
        if not isinstance(identity[stamp], str) or not identity[stamp]:
            raise UpdateError("release identity is missing its %s timestamp" % stamp)
    validate_manifest(manifest, identity)
    return identity, manifest, manifest_bytes


def installation_path():
    """Canonical realpath of this installation (the module's repository root)."""
    return Path(__file__).resolve().parent.parent


def private_state_dir(path, label):
    """Create or verify a private, real, user-owned state directory."""
    if path.is_symlink():
        raise UpdateError("%s path is a symlink; refusing unsafe state" % label)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError("cannot prepare the %s directory; check permissions" % label) from exc
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise UpdateError("%s directory disappeared; check the state directory" % label) from exc
    owner = getattr(os, "geteuid", None)
    if owner is not None:
        owner = owner()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
            raise UpdateError("%s directory is not a private directory of this user" % label)
    if Path(os.path.realpath(path)) != path:
        raise UpdateError("%s directory resolves elsewhere; refusing unsafe state" % label)
    return path


def private_file_checks(path, label):
    """Refuse symlinked, non-regular or user-permission-unsafe state files."""
    if path.is_symlink():
        raise UpdateError("%s path is a symlink; refusing unsafe state" % label)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise UpdateError("%s file disappeared; check the state directory" % label) from exc
    if not stat.S_ISREG(info.st_mode):
        raise UpdateError("%s path is not a private regular file; refusing unsafe state" % label)
    owner = getattr(os, "geteuid", None)
    if owner is not None:
        owner = owner()
        if info.st_uid != owner:
            raise UpdateError("%s file is not owned by this user; refusing unsafe state" % label)
        if info.st_mode & 0o077:
            raise UpdateError("%s file is readable or writable by others; refusing unsafe state" % label)
    return info


def _flock_lock(path):
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = path.lstat()
        owner = getattr(os, "geteuid", None)
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) \
                or (owner is not None and info.st_uid != owner()):
            raise UpdateError("consent lock is not a private regular file; refusing unsafe state")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except UpdateError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise UpdateError("cannot lock the consent state; check permissions") from exc


@contextmanager
def state_transaction(state_dir):
    """Serialize one consent transaction across processes on POSIX."""
    if not IS_POSIX or fcntl is None:
        yield
        return
    lock_path = state_dir / "consent.lock"
    fd = _flock_lock(lock_path)
    try:
        yield
    finally:
        os.close(fd)


def ensure_device_id(state_dir):
    """Load, or create once, the locally generated private device identifier."""
    path = state_dir / "device-id"
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            fd = None
        except OSError as exc:
            raise UpdateError("cannot create the private device id; check permissions") from exc
        if fd is not None:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_hex(16))
            return path.read_text(encoding="utf-8")
    private_file_checks(path, "device id")
    device = path.read_text(encoding="utf-8").strip()
    if not DEVICE_RE.fullmatch(device):
        raise UpdateError("device id is corrupt; refusing unsafe consent state")
    return device


def _fsync_dir(path):
    """Best-effort directory fsync so the rename survives a crash; some file
    systems do not support fsyncing a directory, which is not an error here."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path, payload):
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
            while payload:
                payload = payload[os.write(fd, payload):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(str(tmp), str(path))
        except OSError as exc:
            raise UpdateError("cannot save the consent record; check state directory "
                              "permissions and free space") from exc
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


class _StdTerminal:
    """Default interactive terminal that refuses to prompt without a real TTY."""
    def is_tty(self):
        return IS_POSIX and sys.stdin.isatty() and sys.stdout.isatty()

    def prompt(self, message):
        print(message, end="", flush=True)
        try:
            return input("")
        except EOFError:
            return ""

    def echo(self, message):
        print(message)


def identity_summary(identity):
    lines = [
        "Release identity (does not install anything):",
        "  repository: %s" % identity["repository"],
        "  release id: %s" % identity["releaseId"],
        "  tag:        %s" % identity["tag"],
        "  commit:     %s" % identity["commit"],
        "  asset id:   %s" % identity["assetId"],
        "  manifest:   %s" % identity["manifestDigest"],
        "  published:  %s" % identity["publishedAt"],
        "  asset stamp:%s" % identity["assetUpdatedAt"],
    ]
    return "\n".join(lines)


def read_typed_identity(identity, terminal):
    """Require the exact typed version and full commit on a real TTY."""
    if not terminal.is_tty():
        raise UpdateError("approval needs an interactive terminal; refusing a non-interactive path")
    version = terminal.prompt(VERSION_PROMPT)
    commit = terminal.prompt(COMMIT_PROMPT)
    confirm = terminal.prompt(CONSENT_PROMPT)
    if version != identity["tag"]:
        raise UpdateError("typed version does not exactly match the release tag")
    if commit != identity["commit"]:
        raise UpdateError("typed commit does not exactly match the release commit")
    if confirm != "approve":
        raise UpdateError("consent was not confirmed; nothing was recorded")


def approve(identity, *, state_dir, now, terminal=None):
    """Record one device-local, private, expiring consent after typed confirmation."""
    state_dir = private_state_dir(Path(state_dir), "consent state")
    terminal = terminal if terminal is not None else _StdTerminal()
    digest = identity_digest(identity)
    terminal.echo(identity_summary(identity))
    read_typed_identity(identity, terminal)
    with state_transaction(state_dir):
        device = ensure_device_id(state_dir)
        consent = {
            "schema": CONSENT_SCHEMA,
            "repository": identity["repository"],
            "tag": identity["tag"],
            "commit": identity["commit"],
            "identityDigest": digest,
            "identity": identity,
            "installation": str(installation_path()),
            "device": device,
            "nonce": secrets.token_hex(16),
            "issuedAt": now,
            "expiresAt": now + CONSENT_MAX_AGE,
            "consumed": False,
        }
        _atomic_write(state_dir / "consent.json", json.dumps(consent, indent=1).encode("utf-8"))
    return consent


def _load_consent(state_dir):
    path = state_dir / "consent.json"
    private_file_checks(path, "consent record")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UpdateError("cannot read the consent record; check permissions") from exc
    if len(raw) > MAX_CONSENT_BYTES:
        raise UpdateError("consent record is too large; refusing unsafe state")
    consent = strict_json(raw, "consent record")
    if consent.get("schema") != CONSENT_SCHEMA:
        raise UpdateError("consent record schema is not %s" % CONSENT_SCHEMA)
    if not isinstance(consent.get("nonce"), str) or not NONCE_RE.fullmatch(consent["nonce"]):
        raise UpdateError("consent record nonce is corrupt; refusing unsafe state")
    return consent, path


def consume_consent(identity, *, state_dir, now):
    """Validate and burn one consent: expired, future, mutated, replayed or wrong."""
    state_dir = private_state_dir(Path(state_dir), "consent state")
    with state_transaction(state_dir):
        consent, path = _load_consent(state_dir)
        stored = consent.get("identity")
        if not isinstance(stored, dict) or identity_digest(stored) != consent.get("identityDigest"):
            raise UpdateError("consent record digest does not match its stored identity")
        if identity_digest(identity) != consent.get("identityDigest"):
            raise UpdateError("consent does not match this release identity")
        if consent.get("tag") != identity["tag"] or consent.get("commit") != identity["commit"]:
            raise UpdateError("consent was issued for a different release")
        if consent.get("installation") != str(installation_path()):
            raise UpdateError("consent was recorded for a different installation path")
        if consent.get("device") != ensure_device_id(state_dir):
            raise UpdateError("consent was recorded on a different device")
        issued, expires = consent.get("issuedAt"), consent.get("expiresAt")
        if not isinstance(issued, (int, float)) or not isinstance(expires, (int, float)):
            raise UpdateError("consent record timestamps are corrupt; refusing unsafe state")
        if issued > now:
            raise UpdateError("consent is stamped in the future; refusing unsafe state")
        if expires > issued + CONSENT_MAX_AGE:
            raise UpdateError("consent lifetime exceeds the maximum; refusing unsafe state")
        if now >= expires:
            raise UpdateError("consent has expired; approve the release again")
        if consent.get("consumed") is not False:
            raise UpdateError("consent was already consumed; approve the release again")
        consent["consumed"] = True
        consent["consumedAt"] = now
        _atomic_write(path, json.dumps(consent, indent=1).encode("utf-8"))
    return consent
