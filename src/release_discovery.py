#!/usr/bin/env python3
"""Read-only discovery of published agent-relay releases."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from functools import cmp_to_key
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_URL = "https://api.github.com/repos/Ami-Khokhar/agent-relay/releases?per_page=20"
RELEASE_URL = "https://github.com/Ami-Khokhar/agent-relay/releases/tag/"
MAX_RESPONSE = 512 * 1024
MAX_NOTES = 2000
TIMEOUT = 5
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


def _newer(candidate, installed):
    return _compare_versions(candidate, installed) > 0


def _compare_versions(left_version, right_version):
    a, b = _version(left_version), _version(right_version)
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
    _version(installed)
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
        if version[3] and not include_prereleases:
            continue
        if release.get("prerelease") is True and not include_prereleases:
            continue
        # Only accept metadata with the canonical release URL implied by its tag.
        expected_url = RELEASE_URL + tag
        if release.get("html_url") != expected_url:
            continue
        if _newer(tag, installed):
            raw_notes = release.get("body") if isinstance(release.get("body"), str) else ""
            notes = "".join(char for char in raw_notes
                            if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf"))
            candidates.append((version, {"version": tag, "url": expected_url,
                                        "notes": notes[:MAX_NOTES]}))
    if not candidates:
        return None
    return max(candidates, key=cmp_to_key(
        lambda a, b: _compare_versions(a[1]["version"], b[1]["version"])))[1]


def _cache_path():
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "agent-relay" / "release-notices.json"


def _notify_once(offer):
    path = _cache_path()
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        previous = {}
    if isinstance(previous, dict) and previous.get("version") == offer["version"]:
        return False
    fd = None
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, temp_path = tempfile.mkstemp(prefix="release-notice-", dir=str(path.parent))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump({"version": offer["version"]}, handle)
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
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check for published agent-relay releases; this command never installs or restarts.")
    parser.add_argument("--include-prereleases", action="store_true", help="include explicitly published prereleases")
    parser.add_argument("--notify", action="store_true", help="show a new offer only once per version on this device")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        print("error: cannot read installed version from pyproject.toml", file=sys.stderr)
        return 1
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        print("error: cannot determine installed version", file=sys.stderr)
        return 1
    installed = match.group(1)
    source = (root / ".git").exists()
    try:
        offer = discover(installed, args.include_prereleases)
    except (DiscoveryError, OSError) as exc:
        print("Release check failed: %s" % exc, file=sys.stderr)
        return 1
    install_kind = "source checkout" if source else "non-git installation; update method unknown"
    print("Installed version: %s (%s)" % (installed, install_kind))
    if offer is None:
        print("No newer published %srelease is available." % ("prerelease or stable " if args.include_prereleases else "stable "))
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
        print("Release notes:\n%s%s" % (offer["notes"], "…" if len(offer["notes"]) == MAX_NOTES else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
