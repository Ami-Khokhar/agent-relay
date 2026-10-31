#!/usr/bin/env python3
"""Read-only discovery of published agent-relay releases."""

import argparse
import contextlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unicodedata
from functools import cmp_to_key
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    import fcntl
except ImportError:
    fcntl = None

API_URL = "https://api.github.com/repos/Ami-Khokhar/agent-relay/releases?per_page=20"
RELEASE_URL = "https://github.com/Ami-Khokhar/agent-relay/releases/tag/"
MAX_RESPONSE = 512 * 1024
MAX_NOTES = 2000
MAX_VERSION = 256
# An astral notes character serializes as a 12-character JSON surrogate escape,
# so the read cap must cover the worst-case escaping of a maximum-size offer.
MAX_CACHE = 32 * 1024
OFFER_MAX_AGE = 14 * 24 * 60 * 60
LOCK_TIMEOUT = 5.0
TIMEOUT = 5
_IS_POSIX = os.name == "posix"
VERSION_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


class DiscoveryError(Exception):
    pass


def _version(value):
    match = VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("release version is not semantic versioning")
    major, minor, patch, prerelease = match.groups()
    identifiers = tuple(prerelease.split(".")) if prerelease else ()
    for item in identifiers:
        if item.isdigit() and len(item) > 1 and item[0] == "0":
            raise ValueError("release version has a leading zero")
    return (int(major), int(minor), int(patch), identifiers)


def _compare_parsed(a, b):
    if a[:3] != b[:3]:
        return (a[:3] > b[:3]) - (a[:3] < b[:3])
    x, y = a[3], b[3]
    if not x or not y:
        return (not x) - (not y)
    for left, right in zip(x, y):
        if left == right:
            continue
        ln, rn = left.isdigit(), right.isdigit()
        if ln and rn:
            return (int(left) > int(right)) - (int(left) < int(right))
        if ln != rn:
            return -1 if ln else 1
        return (left > right) - (left < right)
    return (len(x) > len(y)) - (len(x) < len(y))


class _NoRedirect(HTTPRedirectHandler):
    """Disallow every redirect so metadata stays on the canonical host."""
    def http_error_301(self, req, fp, code, msg, headers):
        raise DiscoveryError("release service redirected; refusing non-canonical metadata")

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def fetch_releases():
    request = Request(API_URL, headers={"Accept": "application/vnd.github+json",
                                        "User-Agent": "agent-relay-release-check"})
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            if response.geturl() != API_URL:
                raise DiscoveryError("release service returned a non-canonical URL")
            data = response.read(MAX_RESPONSE + 1)
    except DiscoveryError:
        raise
    except HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise DiscoveryError("release service redirected; refusing non-canonical metadata") from exc
        raise DiscoveryError("release service unavailable (HTTP %d); try again later" % exc.code) from exc
    except TimeoutError as exc:
        raise DiscoveryError("release service timed out; check connectivity and try again") from exc
    except http.client.HTTPException as exc:
        raise DiscoveryError("release service returned an incomplete response; try again later") from exc
    except (URLError, OSError) as exc:
        raise DiscoveryError("release service unavailable; check connectivity and try again") from exc
    if len(data) > MAX_RESPONSE:
        raise DiscoveryError("release metadata exceeded the response size limit")
    try:
        releases = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DiscoveryError("release service returned invalid JSON") from exc
    if not isinstance(releases, list):
        raise DiscoveryError("release service returned an invalid release list")
    return releases


def discover(installed, include_prereleases=False, releases=None):
    installed_version = _version(installed)
    candidates = []
    for release in (fetch_releases() if releases is None else releases):
        if not isinstance(release, dict) or not isinstance(release.get("draft"), bool) \
                or not isinstance(release.get("prerelease"), bool):
            continue
        if release["draft"]:
            continue
        tag = release.get("tag_name")
        try:
            version = _version(tag)
        except (ValueError, TypeError):
            continue
        if len(tag) > MAX_VERSION:
            continue
        if version[3] and not include_prereleases:
            continue
        if release.get("prerelease") is True and not include_prereleases:
            continue
        # Only accept metadata with the canonical release URL implied by its tag.
        expected_url = RELEASE_URL + tag
        if release.get("html_url") != expected_url:
            continue
        if _compare_parsed(version, installed_version) > 0:
            raw_notes = release.get("body") if isinstance(release.get("body"), str) else ""
            notes = "".join(char for char in raw_notes
                            if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf"))
            candidates.append((version, {"version": tag, "url": expected_url,
                                        "notes": notes[:MAX_NOTES]}))
    if not candidates:
        return None
    return max(candidates, key=cmp_to_key(lambda a, b: _compare_parsed(a[0], b[0])))[1]


def _cache_path():
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    if not cache_home.is_absolute():
        cache_home = Path.home() / ".cache"
    return cache_home / "agent-relay" / "release-notices.json"


def installed_version():
    root = Path(__file__).resolve().parent.parent
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError as exc:
        raise DiscoveryError("cannot read installed version from pyproject.toml") from exc
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise DiscoveryError("cannot determine installed version")
    try:
        _version(match.group(1))
    except ValueError as exc:
        raise DiscoveryError("installed version is not semantic versioning; fix the version in pyproject.toml") from exc
    return match.group(1)


def _private_cache():
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.lstat()
        owner = getattr(os, "geteuid", lambda: None)()
        if (not path.parent.is_dir() or path.parent.is_symlink()
                or (owner is not None and parent.st_uid != owner)):
            raise OSError("cache directory is not owned by this user")
        if _IS_POSIX:
            os.chmod(path.parent, 0o700)
    except OSError as exc:
        raise DiscoveryError("cannot create the private release cache; check cache directory permissions") from exc
    return path


_transaction = threading.local()


def _acquire_cache_lock():
    """Hold an exclusive lock on the stable sibling lock file for one transaction."""
    if not _IS_POSIX or fcntl is None:
        return None
    path = _private_cache().with_suffix(".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(path, flags, 0o600)
        info = path.lstat()
        owner = getattr(os, "geteuid", lambda: None)()
        if (path.is_symlink() or not path.is_file()
                or (owner is not None and info.st_uid != owner)):
            raise DiscoveryError("notice cache lock is not a private regular file; check cache directory permissions")
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DiscoveryError("timed out waiting for the private notice cache lock")
                time.sleep(0.05)
    except DiscoveryError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise DiscoveryError("cannot lock the private notice cache; check cache directory permissions") from exc


def _release_cache_lock(fd):
    if fd is not None:
        os.close(fd)


@contextlib.contextmanager
def _cache_transaction():
    """Serialize one read/decision/write cache transaction across processes."""
    if getattr(_transaction, "active", False):
        yield
        return
    fd = _acquire_cache_lock()
    _transaction.active = True
    try:
        yield
    finally:
        _transaction.active = False
        _release_cache_lock(fd)


def _read_cache():
    path = _cache_path()
    try:
        parent = path.parent.lstat()
        info = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    owner = getattr(os, "geteuid", lambda: None)()
    if (not path.parent.is_dir() or path.parent.is_symlink()
            or (owner is not None and parent.st_uid != owner)
            or (_IS_POSIX and parent.st_mode & 0o077)
            or not path.is_file() or path.is_symlink()
            or (owner is not None and info.st_uid != owner)
            or (_IS_POSIX and info.st_mode & 0o077)
            or info.st_size > MAX_CACHE):
        return {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _write_cache(offer, notified_version=None):
    with _cache_transaction():
        previous = _read_cache()
        if notified_version is None:
            notified_version = previous.get("notifiedVersion")
            if notified_version is None and isinstance(previous.get("version"), str):
                notified_version = previous["version"]
        value = {"offer": offer, "notifiedVersion": notified_version}
        path = _private_cache()
        fd = None
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix="release-notice-", dir=str(path.parent))
            if _IS_POSIX:
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump(value, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except OSError as exc:
            raise DiscoveryError("cannot save the private notice cache; check cache directory permissions") from exc
        finally:
            try:
                if fd is not None:
                    os.close(fd)
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError as exc:
                raise DiscoveryError("cannot clean up the private notice cache; check cache directory permissions") from exc
    return value


def _valid_offer(offer, installed):
    if not isinstance(offer, dict):
        return None
    version, url, notes, checked = (offer.get(key) for key in
                                    ("version", "url", "notes", "checkedAt"))
    try:
        parsed = _version(version)
    except (ValueError, TypeError):
        return None
    if (url != RELEASE_URL + version or not isinstance(notes, str) or len(notes) > MAX_NOTES
            or len(version) > MAX_VERSION
            or not isinstance(checked, (int, float)) or isinstance(checked, bool)
            or not math.isfinite(checked)
            or checked > time.time() + 300 or time.time() - checked > OFFER_MAX_AGE
            or _compare_parsed(parsed, _version(installed)) <= 0):
        return None
    cleaned = "".join(char for char in notes
                      if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf"))
    if cleaned != notes:
        return None
    return {"version": version, "url": url, "notes": notes, "checkedAt": checked}


def cached_notice(installed=None):
    """Return one validated, version/link-only notice from the private local cache."""
    try:
        installed = installed or installed_version()
        with _cache_transaction():
            cache = _read_cache()
            offer = _valid_offer(cache.get("offer"), installed)
            if offer is None or cache.get("notifiedVersion") == offer["version"]:
                return None
            _write_cache(offer, offer["version"])
        return {"version": offer["version"], "url": offer["url"]}
    except (DiscoveryError, OSError, TypeError, ValueError):
        return None


def cache_latest_offer(include_prereleases=False, installed=None):
    """Fetch and cache a bounded offer without recording that a user saw it."""
    installed = installed or installed_version()
    offer = discover(installed, include_prereleases)
    value = None if offer is None else {**offer, "checkedAt": time.time()}
    _write_cache(value)
    return installed, offer


def _notify_once(offer):
    with _cache_transaction():
        cache = _read_cache()
        if cache.get("notifiedVersion") == offer["version"] or cache.get("version") == offer["version"]:
            return False
        _write_cache({**offer, "checkedAt": time.time()}, offer["version"])
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check for published agent-relay releases; this command never installs or restarts.")
    parser.add_argument("--include-prereleases", action="store_true", help="include explicitly published prereleases")
    parser.add_argument("--notify", action="store_true", help="show a new offer only once per version on this device")
    parser.add_argument("--cache-offer", action="store_true",
                        help="cache release metadata for the MCP client without marking it shown")
    args = parser.parse_args(argv)
    if args.notify and args.cache_offer:
        parser.error("--notify and --cache-offer cannot be used together")
    try:
        installed = installed_version()
    except DiscoveryError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    try:
        if args.cache_offer:
            installed, offer = cache_latest_offer(args.include_prereleases, installed)
        else:
            offer = discover(installed, args.include_prereleases)
    except (DiscoveryError, OSError) as exc:
        print("Release check failed: %s" % exc, file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent.parent
    source = (root / ".git").exists()
    install_kind = "source checkout" if source else "non-git installation; update method unknown"
    print("Installed version: %s (%s)" % (installed, install_kind))
    if offer is None:
        if args.cache_offer:
            print("Cached release check: no newer release is available.")
        print("No newer published %srelease is available." % ("prerelease or stable " if args.include_prereleases else "stable "))
        return 0
    if args.cache_offer:
        print("Cached release offer: %s" % offer["version"])
        return 0
    if args.notify:
        try:
            if not _notify_once(offer):
                return 0
        except DiscoveryError as exc:
            print("Release notice failed: %s" % exc, file=sys.stderr)
            return 1
    print("Available version: %s" % offer["version"])
    print("Release: %s" % offer["url"])
    if offer["notes"]:
        print("Release notes:\n%s" % offer["notes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
