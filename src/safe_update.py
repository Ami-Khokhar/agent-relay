#!/usr/bin/env python3
"""Safe update CLI: inspect a published release and record device-local consent.

This is the first consumer of ``release_identity``. ``inspect`` resolves and
validates a stable release identity (metadata only) and ``approve`` adds an
interactive, private, 15-minute device-local consent record. Nothing is
installed, activated or executed by these commands; later stages will extend
the same CLI to consume the consent for activation.
"""

import argparse
import contextlib
import http.client
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

import release_identity
from release_identity import UpdateError

STATE_LABEL = "release update consent"
JSON_ACCEPT = "application/vnd.github+json"
# The manifest asset is file content: GitHub answers the JSON Accept header with
# asset metadata, so it must be requested with the octet-stream Accept header.
ASSET_ACCEPT = "application/octet-stream"
# GitHub serves release assets by 302ing to one of these signed download hosts;
# any other redirect target is an uncanonical identity and refused.
ASSET_REDIRECT_HOSTS = frozenset(("github.com", "objects.githubusercontent.com",
                                  "release-assets.githubusercontent.com"))


class _Redirected(Exception):
    """Carries an un-followed redirect target out of the opener for inspection."""

    def __init__(self, location):
        super().__init__(location)
        self.location = location


class _CaptureRedirect(HTTPRedirectHandler):
    """Raise with the redirect target instead of following it automatically."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's 30x handlers already make newurl absolute for this request.
        raise _Redirected(newurl)


def _fetch(url):
    """One bounded HTTPS request against the canonical GitHub API host.

    Release and tag metadata must stay on the canonical API host with no
    redirect at all. The manifest asset is requested with the octet-stream
    Accept header, and GitHub answers it with one redirect to a signed download
    host: exactly one redirect there is followed, and only to a canonical
    GitHub download host; every other redirect is refused.
    """
    if not url.startswith(release_identity.API_BASE + "/"):
        raise UpdateError("refusing a non-canonical release identity request")
    asset = url.startswith(release_identity.ASSET_URL)
    accept = ASSET_ACCEPT if asset else JSON_ACCEPT
    current, redirects_left = url, 1 if asset else 0
    while True:
        request = release_identity.Request(
            current, headers={"Accept": accept,
                              "User-Agent": "agent-relay-release-update"})
        handler = _CaptureRedirect() if redirects_left else release_identity._NoRedirect()
        try:
            with build_opener(handler).open(request, timeout=5) as response:
                if response.geturl() != current:
                    raise UpdateError("release service returned a non-canonical URL")
                data = response.read(release_identity.release_discovery.MAX_RESPONSE + 1)
        except _Redirected as redirected:
            if not redirects_left:
                raise UpdateError("release service redirected; refusing non-canonical identity") from None
            redirects_left -= 1
            target = urlsplit(redirected.location)
            if target.scheme != "https" or (target.hostname or "") not in ASSET_REDIRECT_HOSTS:
                raise UpdateError("release asset redirected to a non-canonical host; "
                                  "refusing uncanonical identity") from None
            current = redirected.location
            continue
        except HTTPError as exc:
            if exc.code == 404:
                raise UpdateError("no published stable release matches this tag; check the tag name") from exc
            if exc.code in (301, 302, 303, 307, 308):
                raise UpdateError("release service redirected; refusing non-canonical identity") from exc
            raise UpdateError("release service unavailable (HTTP %d); try again later" % exc.code) from exc
        except (http.client.HTTPException, TimeoutError) as exc:
            raise UpdateError("release service timed out or hung up; try again later") from exc
        except (URLError, OSError) as exc:
            raise UpdateError("release service unavailable; check connectivity and try again") from exc
        return data


def _resolve(tag):
    identity, manifest, _bytes = release_identity.resolve_release(tag, fetch_json=_fetch)
    return identity, manifest


def _state_dir():
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "agent-relay"


def cmd_inspect(tag):
    identity, manifest = _resolve(tag)
    print("Release identity validated (no release will be installed):")
    print(release_identity.identity_summary(identity))
    print("Manifest schema: %s" % manifest["schema"])
    return 0


def cmd_approve(tag):
    identity, _manifest = _resolve(tag)
    consent = release_identity.approve(identity, state_dir=_state_dir(), now=time.time())
    print("Consent recorded for %s until %s (nonce %s)." %
          (consent["tag"], consent["expiresAt"], consent["nonce"][:8]))
    print("Nothing was installed; activation must consume this consent later.")
    return 0


def cmd_consume(tag):
    identity, _manifest = _resolve(tag)
    consent = release_identity.consume_consent(identity, state_dir=_state_dir(), now=time.time())
    print("Consent validated and consumed for %s at commit %s." % (consent["tag"], consent["commit"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect a published agent-relay release or record device-local approval; never installs or activates.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("inspect", cmd_inspect, "resolve and validate a stable release identity (metadata only)"),
        ("approve", cmd_approve, "interactively record a private, expiring device-local consent"),
        ("consume", cmd_consume, "validate and burn the recorded consent for a release"),
    ):
        sub = subcommands.add_parser(name, help=help_text)
        sub.add_argument("tag", help="exact release tag to %s, e.g. 1.2.3" % name)
    args = parser.parse_args(argv)
    handlers = {"inspect": cmd_inspect, "approve": cmd_approve, "consume": cmd_consume}
    try:
        return handlers[args.command](args.tag)
    except UpdateError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: update state could not be used: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
