#!/usr/bin/env python3
"""Client side of the authenticated shared-lock drain (#119, issue #156).

The running relay holds one drain at a time: while it is held, the server
refuses every NEW external admission (tasks, workflow submissions and
resumes) but lets already accepted work — running tasks, queued tasks and
the child tasks of live workflows — finish normally. An updater therefore
cannot restart while work is mid-flight, and the server, not the updater,
acknowledges that the held drain has reached quiescence.

This module is the protocol client the safe update CLI uses. It exposes the
three verbs the server route ``/v1/admin/update/drain`` serves and nothing
else: acquire (POST), status (GET) and release (DELETE), each with a
transaction-bound, opaque capability the relay itself issued. Wrong
revisions, malformed requests and unknown transactions are always explicit
errors — they never make a drain look idle or free.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from release_identity import UpdateError

# The three admin verbs and the unauthenticated metadata source for the
# revision the acquire must name.
DRAIN_PATH = "/v1/admin/update/drain"
HEALTH_PATH = "/healthz"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
TRANSACTION_RE = re.compile(r"[A-Za-z0-9_.~-]{1,128}")
# A capability is 64 hex the relay generated; the client never validates or
# chooses one, it only carries back what the acquire answered.
# Bounded waits: acquiring never turns into an unbounded stall, and waiting
# for quiescence always reports instead of hanging.
DEFAULT_ACQUIRE_WAIT_S = 15.0
DEFAULT_QUIESCE_WAIT_S = 300.0
POLL_S = 0.2
HTTP_TIMEOUT_S = 5.0
LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))


class DrainError(UpdateError):
    """A drain verb could not be completed; ``code`` names the server's answer."""

    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.status = status
        # None unless the raiser attaches the last acknowledged state; callers
        # must not print counts they never received.
        self.last_status = None


def relay_base_url():
    """The running relay's base URL: AGENT_RELAY_URL, else A2A_RELAY_URL."""
    for name in ("AGENT_RELAY_URL", "A2A_RELAY_URL"):
        value = os.environ.get(name)
        if value:
            return value.rstrip("/")
    return None


def relay_token():
    """The relay API token from the environment, or None when it is absent."""
    for name in ("AGENT_RELAY_TOKEN", "A2A_RELAY_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def new_transaction_id():
    """A fresh, opaque local name for one update transaction."""
    return "drain-" + secrets.token_hex(8)


def _check_loopback(base_url):
    host = (urlparse.urlsplit(base_url).hostname or "").strip("[]").lower()
    if host not in LOOPBACK_HOSTS:
        # The drain endpoint exists to stop a restart racing new admissions;
        # it binds nothing but this one local service.
        raise DrainError("drain_non_loopback",
                         "the drain protocol protects only a loopback relay; "
                         "refusing base URL %r" % base_url)


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Never follow a redirect: the bearer token must reach only the relay.

    urllib's default follows up to ten redirects and re-sends the request
    headers to each target, which would hand the token that reopens admission
    (and the drain capability) to wherever a hijacked answer points.
    """

    def redirect_request(self, *args, **kwargs):
        return None


# Built once: one opener policy for every verb this module sends.
_OPENER = urlrequest.build_opener(_NoRedirect)


def _request(method, base_url, path, *, token=None, body=None, query=None,
             timeout=HTTP_TIMEOUT_S):
    """One bounded JSON request; returns ``(status, body)`` for every answer.

    HTTP errors are data here, not exceptions in the protocol sense: the caller
    maps server error codes to its own decisions, so this re-raises nothing but
    a transport failure. A redirect is a transport failure: it is never
    followed, so no credential is forwarded.
    """
    url = base_url + path
    if query:
        url += "?" + urlparse.urlencode(query)
    data = None
    headers = {}
    if token:
        headers["authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urlrequest.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise DrainError("drain_unreachable",
                             "the relay answered a redirect (HTTP %d); refusing to follow "
                             "it with the bearer token attached" % exc.code)
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {}
    except (urlerror.URLError, OSError, TimeoutError,
            json.JSONDecodeError, ValueError) as exc:
        raise DrainError("drain_unreachable", "the relay could not be reached: %s" % exc)


def _server_revision(base_url):
    """The exact commit the running relay reports, from its open healthz."""
    status, body = _request("GET", base_url, HEALTH_PATH, token=None)
    if status != 200 or not isinstance(body, dict):
        raise DrainError("drain_revision_unknown", "the relay's healthz gave no revision")
    revision = body.get("revision")
    if isinstance(revision, dict):
        revision = revision.get("commit")
    if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision):
        raise DrainError("drain_revision_unknown",
                         "the relay runs an unknown revision; it cannot name an exact "
                         "starting revision for a drain")
    return revision


def _validate_transaction(transaction_id):
    if not isinstance(transaction_id, str) or not TRANSACTION_RE.fullmatch(transaction_id):
        raise DrainError("invalid_transaction",
                         "transactionId must match [A-Za-z0-9_.~-] and be at most 128 "
                         "characters")


def acquire_drain(base_url, transaction_id, *, token, revision=None,
                  wait_s=DEFAULT_ACQUIRE_WAIT_S):
    """Acquire the shared drain, remembering this exact revision as the start.

    Bounded wait: while another transaction holds the drain, acquire is
    retried until ``wait_s`` elapses. A revision mismatch, an unreachable
    relay or a revision the relay does not run are refused at once, never
    retried and never mistaken for a free drain. Returns the server's answer,
    which carries this transaction's opaque ``capability``.

    The retry is safe for this transaction: if the acquire was applied and its
    answer was lost, naming the same ``transactionId`` again returns the same
    hold and the capability the relay already minted, so a lost response never
    leaves a hold nobody can release. This call names itself once, in
    ``requestId``, and repeats that name on every attempt; the relay keeps it
    with the hold and answers ``recovered: true`` when a reissue recovers the
    very request that created it. The answer therefore carries three facts:
    the relay's ``reacquired`` (this answer replayed a hold instead of making
    one), its ``recovered`` (the replay recovers this call's own earlier
    attempt, whose answer was lost), and this client's ``createdHere`` (this
    call created the hold, or may have) — the only flag on which a caller
    releases. A call whose every attempt was answered ``reacquired`` without
    ``recovered`` is a pure replay of someone else's hold.
    """
    _check_loopback(base_url)
    _validate_transaction(transaction_id)
    revision = _server_revision(base_url) if revision is None else revision
    deadline = time.monotonic() + wait_s
    # One name for this call, not one per request: a request that landed
    # without an answer keeps its name, so the retry can be recognised as the
    # creator's recovery instead of a second caller's inspection.
    request_id = secrets.token_hex(16)
    while True:
        try:
            status, body = _request("POST", base_url, DRAIN_PATH, token=token,
                                    body={"transactionId": transaction_id,
                                          "revision": revision,
                                          "requestId": request_id})
        except DrainError as exc:
            if exc.code == "drain_unreachable" and time.monotonic() < deadline:
                time.sleep(POLL_S)
                continue
            if exc.code == "drain_unreachable":
                # The acquire may have been applied before the transport died;
                # repeating the same transactionId recovers its capability.
                raise DrainError("drain_unreachable",
                                 "%s; if the acquire reached the relay its hold stands — "
                                 "repeat the acquire with --transaction-id %s to receive "
                                 "that capability again (a replay is marked reacquired and "
                                 "is never released here), or run `python3 src/safe_update.py "
                                 "recover-abort %s` if it prepared an activation"
                                 % (exc, transaction_id, transaction_id))
            raise
        if status == 200:
            if isinstance(body, dict):
                # ``reacquired`` and ``recovered`` are the relay's facts about
                # this answer; ``createdHere`` is this call's fact about the hold.
                body["reacquired"] = body.get("reacquired") is True
                body["recovered"] = body.get("recovered") is True
                body["createdHere"] = body["reacquired"] is not True or body["recovered"]
            return body
        code = (body or {}).get("error")
        if status == 409 and code == "drain_held" and time.monotonic() < deadline:
            time.sleep(POLL_S)
            continue
        if status == 409 and code == "drain_held":
            # Not this transaction's hold: name it, the fact that repeating only
            # inspects it, and the command that really ends it.
            holder = (body or {}).get("heldBy") or "another transaction"
            raise DrainError(
                "drain_held",
                "the drain is held by transaction %s. Repeating the acquire with "
                "--transaction-id %s only inspects that hold (it is answered as "
                "reacquired and is never released here); release it with its owner's "
                "recovery: `python3 src/safe_update.py recover-abort %s` for a "
                "prepared record, otherwise from the process that holds it. "
                "Admission stays closed until then" % (holder, holder, holder),
                status=status)
        raise DrainError(code or ("drain_refused_%d" % status),
                         "acquire was refused: %s" % (body or {}).get("message", status),
                         status=status)


def drain_status(base_url, transaction_id, *, token, revision, capability,
                 timeout=HTTP_TIMEOUT_S):
    """Ask the relay for the state of one held drain.

    Every caller-supplied field is verified by the relay; a mismatch is an
    error, and an error never means ``idle``: only the relay's own well-formed
    answer carries the ``quiesced`` verdict the server computed.
    """
    _check_loopback(base_url)
    _validate_transaction(transaction_id)
    status, body = _request("GET", base_url, DRAIN_PATH, token=token, timeout=timeout,
                            query={"transactionId": transaction_id,
                                   "revision": revision, "capability": capability})
    if status != 200:
        raise DrainError((body or {}).get("error", "drain_status_%d" % status),
                         "status was refused: %s" % (body or {}).get("message", status),
                         status=status)
    return body


def wait_for_quiescence(base_url, transaction_id, *, token, revision, capability,
                        wait_s=DEFAULT_QUIESCE_WAIT_S):
    """Poll the held drain until the relay acknowledges quiescence, boundedly.

    Returns the last status body. On timeout it raises, naming the counts the
    relay last reported, so a caller releases the drain and reports rather
    than guessing from /healthz.
    """
    deadline = time.monotonic() + wait_s
    last = None
    while True:
        try:
            last = drain_status(base_url, transaction_id, token=token,
                                revision=revision, capability=capability)
        except DrainError as exc:
            if exc.code in ("drain_unreachable",) and time.monotonic() < deadline:
                time.sleep(POLL_S)
                continue
            raise
        if last.get("quiesced"):
            return last
        if time.monotonic() >= deadline:
            counts = "active=%(active)s queued=%(queued)s workflows=%(workflows)s" % last
            failure = DrainError("drain_not_quiesced",
                                 "the drain did not quiesce within %.0fs (%s)"
                                 % (wait_s, counts))
            # The last acknowledged state travels with the error, so a caller
            # releases an informed hold and reports real numbers, not guesses.
            failure.last_status = last
            raise failure
        time.sleep(POLL_S)


def release_drain(base_url, transaction_id, *, token, revision, capability,
                  timeout=HTTP_TIMEOUT_S):
    """Release the held drain with the exact capability that acquired it.

    A replayed or foreign capability is refused; the relay then still holds
    the drain and reopening is the explicit release's business alone. Callers
    release only a hold their own acquire call created (``createdHere``): a
    hold reissued to a later call belongs to the transaction that already
    owned it, and only that transaction's own recovery may end it.
    """
    _check_loopback(base_url)
    _validate_transaction(transaction_id)
    status, body = _request("DELETE", base_url, DRAIN_PATH, token=token, timeout=timeout,
                            body={"transactionId": transaction_id,
                                  "revision": revision, "capability": capability})
    if status != 200:
        raise DrainError((body or {}).get("error", "drain_release_%d" % status),
                         "release was refused: %s" % (body or {}).get("message", status),
                         status=status)
    return body
