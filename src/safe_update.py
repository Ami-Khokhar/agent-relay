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
from urllib.request import build_opener

import release_identity
from release_identity import UpdateError

STATE_LABEL = "release update consent"


def _fetch(url):
    """One bounded, redirect-free HTTPS request against the canonical API host."""
    if not url.startswith(release_identity.API_BASE + "/"):
        raise UpdateError("refusing a non-canonical release identity request")
    request = release_identity.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "agent-relay-release-update"})
    opener = build_opener(release_identity._NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            if response.geturl() != url:
                raise UpdateError("release service returned a non-canonical URL")
            data = response.read(release_identity.release_discovery.MAX_RESPONSE + 1)
    except UpdateError:
        raise
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
