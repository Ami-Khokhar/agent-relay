#!/usr/bin/env python3
"""Command line client for agent-artifacts.

Any coding agent can publish a page, attach its session, wait for the person's
comments, and answer them. Comments can be addressed to one agent with ``@name``;
an addressed comment waits in the queue until that agent next reads its inbox.

    python3 src/artifact_cli.py publish report.html --title "Q4 review"
    python3 src/artifact_cli.py inbox q4-review --wait 900 --json
    python3 src/artifact_cli.py sessions q4-review

Every command prints human text by default and JSON with --json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit

SERVER = Path(__file__).resolve().parent / "artifact_server.py"
HANDLE_OK = re.compile(r"@[a-z0-9][a-z0-9._-]{0,39}")


# ---------------------------------------------------------------- environment


def base_url() -> str:
    return (os.environ.get("AGENT_ARTIFACTS_URL") or "http://127.0.0.1:43126").rstrip("/")


def state_root() -> Path:
    explicit = os.environ.get("AGENT_ARTIFACTS_DIR") or os.environ.get("A2A_ARTIFACTS_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "agent-relay" / "artifacts"


def guess_agent() -> str:
    explicit = os.environ.get("AI_AGENT") or os.environ.get("AGENT_NAME")
    if explicit:
        return explicit.lower()
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_HOME"):
        return "codex"
    if os.environ.get("OPENCODE"):
        return "opencode"
    if os.environ.get("PI_SESSION_ID") or os.environ.get("PI_CODING_AGENT"):
        return "pi"
    return "agent"


def guess_model() -> str:
    for name in ("AGENT_ARTIFACTS_MODEL", "PI_MODEL", "CLAUDE_MODEL", "ANTHROPIC_MODEL",
                 "OPENAI_MODEL", "CODEX_MODEL", "GEMINI_MODEL"):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def guess_handle() -> str:
    explicit = os.environ.get("AGENT_ARTIFACTS_AS")
    if explicit:
        return normalize_handle(explicit)
    agent = re.sub(r"[^a-z0-9]+", "-", guess_agent()).strip("-") or "agent"
    family = re.sub(r"[^a-z0-9]+", "-", guess_model().split("-")[0].lower()).strip("-")
    handle = f"{agent}-{family}" if family and family != agent else agent
    return normalize_handle(handle)


def normalize_handle(value) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise SystemExit("artifact: a handle is required, e.g. --as @codex")
    if not text.startswith("@"):
        text = "@" + text
    if not HANDLE_OK.fullmatch(text):
        raise SystemExit(f"artifact: invalid handle {value!r}; use @name with a-z, 0-9, dot, dash")
    return text


def guess_session() -> str:
    for name in ("AGENT_ARTIFACTS_SESSION", "PI_SESSION_ID", "CLAUDE_SESSION_ID",
                 "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def session_for(slug: str, handle: str, new: bool = False) -> str:
    """Reuse one CLI session per artifact and handle so repeat calls do not pile up.

    The agent's own session id (PI_SESSION_ID and friends) is only used for the
    handle this process derives for itself. Naming another agent with --as must
    never borrow this process's session.
    """
    env_session = guess_session()
    if env_session and handle == guess_handle():
        return env_session
    if new:
        return "s-" + secrets.token_hex(4)
    cache = state_root() / slug / "cli-sessions.json"
    try:
        with open(cache, "r", encoding="utf-8") as handle_file:
            data = json.load(handle_file)
    except (OSError, ValueError):
        data = {}
    if isinstance(data.get(handle), str) and data[handle]:
        return data[handle]
    session = "s-" + secrets.token_hex(4)
    data[handle] = session
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as handle_file:
            json.dump(data, handle_file, indent=2)
    except OSError:
        pass
    return session


# ---------------------------------------------------------------------- http


def call(method: str, path: str, payload=None, timeout=30):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urlrequest.Request(base_url() + path, data=data, method=method)
    request.add_header("x-artifacts-client", "1")
    if data is not None:
        request.add_header("content-type", "application/json")
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"error": raw}
        raise SystemExit(f"artifact: {exc.code} {parsed.get('error')}"
                         + (f": {parsed.get('message')}" if parsed.get("message") else ""))
    except (urlerror.URLError, ConnectionError, TimeoutError) as exc:
        raise SystemExit(f"artifact: cannot reach {base_url()} ({exc}). "
                         "Start it with: python3 src/artifact_server.py")


def healthy(timeout=1.0) -> bool:
    try:
        with urlrequest.urlopen(base_url() + "/healthz", timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not up"
        return False


def ensure_server(allow_start=True):
    if healthy():
        return
    parsed = urlsplit(base_url())
    if not allow_start or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"artifact: {base_url()} is not running")
    port = parsed.port or 43126
    log_path = state_root() / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env={**os.environ, "AGENT_ARTIFACTS_PORT": str(port),
                 "AGENT_ARTIFACTS_HOST": parsed.hostname or "127.0.0.1"},
            start_new_session=True,
        )
    deadline = time.time() + 8
    while time.time() < deadline:
        if healthy(0.4):
            return
        time.sleep(0.1)
    tail = ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        tail = "\n".join(lines[-6:])
    except OSError:
        pass
    hint = ""
    if "Address already in use" in tail:
        hint = (f"\nport {port} is taken; set AGENT_ARTIFACTS_PORT or AGENT_ARTIFACTS_URL "
                "to a free port")
    raise SystemExit(f"artifact: server did not start{hint}\n{tail}")


# ------------------------------------------------------------------- printing


def emit(value, as_json, human):
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    else:
        print(human(value))


def fmt_delivery(comment):
    parts = []
    for item in comment.get("delivery") or []:
        state = item.get("state")
        label = {"claimed": "delivered", "queued": "active, not taken yet",
                 "waiting": "awaits that agent", "resolved": "closed"}.get(state, state)
        parts.append(f"{item['handle']} {label}")
    return ", ".join(parts)


def fmt_comment(comment, index=None):
    head = f"[{comment['id']}]"
    if index is not None:
        head = f"{index}. [{comment['id']}]"
    anchor = comment.get("anchor") or {}
    lines = [f"{head} {comment.get('author', 'owner')} · v{comment.get('version')}"
             f" · {comment.get('created', '')} · {comment.get('status', 'open')}"]
    to = fmt_delivery(comment)
    if to:
        lines.append(f"   to: {to}")
    if anchor.get("quote"):
        lines.append(f"   on: {anchor['quote'][:160]}")
    if anchor.get("selector"):
        lines.append(f"   at: {anchor['selector'][:160]}")
    lines.append("   " + (comment.get("body") or "").replace("\n", "\n   "))
    for reply in comment.get("replies") or []:
        lines.append(f"   ↳ {reply.get('author')}: {reply.get('body')}")
    return "\n".join(lines)


def fmt_sessions(sessions, handles, origin=None):
    if not sessions:
        return "no sessions attached"
    lines = []
    for record in sessions:
        mark = "active" if record.get("live") else "idle"
        role = " (origin)" if origin and origin.get("session") == record.get("id") else ""
        lines.append(f"{record['handle']:<18} {record['id']:<14} {mark:<7}{role}"
                     f" attached {record.get('attachedAt', '')}")
    return "\n".join(lines)


# ------------------------------------------------------------------- commands


def cmd_publish(args):
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise SystemExit(f"artifact: no such file: {path}")
    html = path.read_text(encoding="utf-8", errors="replace")
    ensure_server()
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    payload = {"html": html, "as": handle, "cwd": os.getcwd()}
    if args.title:
        payload["title"] = args.title
    if args.slug:
        payload["slug"] = args.slug
    if args.note:
        payload["note"] = args.note
    payload["session"] = session_for(args.slug or "", handle, new=args.new_session) \
        if args.slug else (guess_session() or "s-" + secrets.token_hex(4))
    result = call("POST", "/api/artifacts", payload, timeout=90)
    emit(result, args.json, lambda r: (
        f"published v{r['version']} · {r['slug']} · as {handle}\n"
        f"review: {r['url']}\n"
        f"raw:    {r['raw']}\n"
        f"open it, comment on elements, then run:\n"
        f"  python3 src/artifact_cli.py inbox {r['slug']} --wait 900 --json"))


def cmd_list(args):
    ensure_server()
    data = call("GET", "/api/artifacts")
    items = data.get("artifacts") or []
    emit(data, args.json, lambda d: "\n".join(
        f"{item['slug']:<30} v{item['currentVersion']:<3} {item['openComments']} open  "
        f"{' '.join(item.get('handles') or []) or '-'}  {item['title']}"
        for item in items) or "no artifacts yet")


def cmd_map(args):
    ensure_server()
    data = call("GET", "/api/artifacts")
    items = data.get("artifacts") or []
    emit(data, args.json, lambda d: "\n\n".join(
        f"{item['slug']}  v{item['currentVersion']}  {item['openComments']} open  {item['title']}\n"
        + fmt_sessions(item.get("sessions") or [], item.get("handles") or [], item.get("origin"))
        for item in items) or "no artifacts yet")


def cmd_show(args):
    ensure_server()
    artifact = call("GET", "/api/artifacts/" + quote(args.slug))
    emit(artifact, args.json, lambda a: "\n".join(
        [f"{a['title']}  ({a['slug']})  v{a['currentVersion']}",
         f"review: {a['url']}",
         f"sessions: {', '.join(a.get('handles') or []) or 'none'}",
         "versions:"]
        + [f"  v{v['n']}  {v['created']}  {v['bytes']} bytes  {v['sha256'][:12]}"
           + (f"  by {v['by']}" if v.get("by") else "")
           for v in a["versions"]]
        + [f"comments: {len(a.get('comments') or [])}"]))


def cmd_attach(args):
    ensure_server()
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    session = args.session or session_for(args.slug, handle, new=args.new_session)
    result = call("POST", f"/api/artifacts/{quote(args.slug)}/sessions",
                  {"as": handle, "session": session, "cwd": os.getcwd()})
    emit(result, args.json, lambda r: (
        f"attached {handle} as {r['session']['id']}\n"
        + fmt_sessions(r["sessions"], [], None)
        + f"\n\nreview: {r['url']}"))


def cmd_detach(args):
    ensure_server()
    sessions = call("GET", f"/api/artifacts/{quote(args.slug)}/sessions").get("sessions") or []
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    session = args.session
    if not session:
        for record in sessions:
            if record["handle"] == handle and record.get("live"):
                session = record["id"]
                break
    if not session:
        for record in sessions:
            if record["handle"] == handle:
                session = record["id"]
                break
    if not session:
        raise SystemExit(f"artifact: no session for {handle} on {args.slug}")
    result = call("DELETE", f"/api/artifacts/{quote(args.slug)}/sessions/{quote(session)}")
    emit(result, args.json, lambda r: f"detached {session}")


def cmd_sessions(args):
    ensure_server()
    data = call("GET", f"/api/artifacts/{quote(args.slug)}/sessions")
    emit(data, args.json, lambda d: fmt_sessions(d["sessions"], d.get("handles") or [],
                                                 d.get("origin")))


def cmd_inbox(args):
    ensure_server()
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    session = args.session or session_for(args.slug, handle)
    deadline = time.time() + max(0, args.wait)
    while True:
        remaining = deadline - time.time()
        chunk = int(min(30, max(1, remaining))) if args.wait else 0
        payload = {"as": handle, "session": session}
        if args.all:
            payload["all"] = True
        if chunk:
            payload["wait"] = chunk
        data = call("POST", f"/api/artifacts/{quote(args.slug)}/inbox", payload,
                    timeout=chunk + 30)
        comments = data.get("comments") or []
        if comments or remaining <= 0 or not args.wait:
            break
    emit(data, args.json, lambda d: "\n\n".join(
        fmt_comment(comment, index + 1) for index, comment in enumerate(comments))
        or f"no new comments for {handle}")


def cmd_wait(args):
    """Block until addressed comments arrive, in 30 second long polls."""
    ensure_server()
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    session = args.session or session_for(args.slug, handle)
    deadline = time.time() + args.timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            emit({"slug": args.slug, "handle": handle, "comments": []}, args.json,
                 lambda d: f"no comments for {handle} on {args.slug} within {args.timeout}s")
            return
        chunk = int(min(30, max(1, remaining)))
        payload = {"as": handle, "session": session, "wait": chunk}
        if args.all:
            payload["all"] = True
        data = call("POST", f"/api/artifacts/{quote(args.slug)}/inbox", payload,
                    timeout=chunk + 30)
        comments = data.get("comments") or []
        if comments:
            emit(data, args.json, lambda d: "\n\n".join(
                fmt_comment(comment, index + 1) for index, comment in enumerate(comments)))
            return


def cmd_comments(args):
    ensure_server()
    query = "?open=1" if args.open else ""
    if args.since is not None:
        query += ("&" if query else "?") + f"since={args.since}"
    data = call("GET", f"/api/artifacts/{quote(args.slug)}/comments{query}")
    comments = data.get("comments") or []
    emit(data, args.json, lambda d: "\n\n".join(
        fmt_comment(comment, index + 1) for index, comment in enumerate(comments))
        or "no comments")


def cmd_reply(args):
    ensure_server()
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    result = call("POST", f"/api/comments/{quote(args.comment_id)}/reply",
                  {"body": args.body, "author": handle})
    emit(result, args.json, lambda r: f"replied to {args.comment_id} as {handle}")


def cmd_release(args):
    ensure_server()
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    result = call("POST", f"/api/comments/{quote(args.comment_id)}/release",
                  {"author": handle})
    emit(result, args.json, lambda r: f"released {args.comment_id} from {handle}")


def cmd_status(args):
    ensure_server()
    author = normalize_handle(args.as_) if args.as_ else guess_handle()
    result = call("POST", f"/api/comments/{quote(args.comment_id)}/status",
                  {"status": args.status, "author": author})
    emit(result, args.json, lambda r: f"{args.comment_id}: {r['status']}")


def cmd_open(args):
    ensure_server()
    artifact = call("GET", "/api/artifacts/" + quote(args.slug))
    url = artifact["url"]
    if args.raw:
        url = artifact["rawBase"] + "/raw?v=" + str(artifact["currentVersion"])
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    elif os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", url], check=False)
    emit({"url": url}, args.json, lambda r: r["url"])


def cmd_rm(args):
    ensure_server()
    result = call("DELETE", "/api/artifacts/" + quote(args.slug))
    emit(result, args.json, lambda r: f"deleted {r['deleted']}")


def cmd_whoami(args):
    handle = normalize_handle(args.as_) if args.as_ else guess_handle()
    payload = {"handle": handle, "agent": guess_agent(), "model": guess_model(),
               "session": guess_session() or None, "url": base_url()}
    emit(payload, args.json, lambda p: (
        f"handle:  {p['handle']}\nagent:   {p['agent']}\nmodel:   {p['model'] or '-'}\n"
        f"session: {p['session'] or '(generated per artifact on first use)'}\nserver:  {p['url']}"))


def cmd_serve(args):
    os.execv(sys.executable, [sys.executable, str(SERVER)])


# --------------------------------------------------------------------- parser


def build_parser():
    parser = argparse.ArgumentParser(prog="artifact", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    # Agents write --json after the subcommand as often as before it, so accept both.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--as", dest="as_", help="agent handle, e.g. @codex")
    common.add_argument("--session", help="session id; one is derived when omitted")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, **kwargs):
        return subparsers.add_parser(name, parents=[common], **kwargs)

    publish = add("publish", help="publish an HTML file as a reviewable artifact")
    publish.add_argument("file")
    publish.add_argument("--title")
    publish.add_argument("--slug", help="reuse a slug to publish a new version")
    publish.add_argument("--note", help="why this version exists")
    publish.add_argument("--new-session", action="store_true")
    publish.set_defaults(func=cmd_publish)

    listing = add("list", help="list artifacts")
    listing.set_defaults(func=cmd_list)

    mapping = add("map", help="artifacts with their attached sessions")
    mapping.set_defaults(func=cmd_map)

    show = add("show", help="show one artifact and its versions")
    show.add_argument("slug")
    show.set_defaults(func=cmd_show)

    attach = add("attach", help="attach this session to an existing artifact")
    attach.add_argument("slug")
    attach.add_argument("--new-session", action="store_true")
    attach.set_defaults(func=cmd_attach)

    detach = add("detach", help="detach a session from an artifact")
    detach.add_argument("slug")
    detach.set_defaults(func=cmd_detach)

    sessions = add("sessions", help="who is attached and who is active")
    sessions.add_argument("slug")
    sessions.set_defaults(func=cmd_sessions)

    inbox_cmd = add("inbox", help="claim the comments addressed to this agent")
    inbox_cmd.add_argument("slug")
    inbox_cmd.add_argument("--wait", type=int, default=0, help="seconds to wait for new comments")
    inbox_cmd.add_argument("--all", action="store_true",
                           help="also take comments addressed to other agents")
    inbox_cmd.set_defaults(func=cmd_inbox)

    wait = add("wait", help="block until addressed comments arrive")
    wait.add_argument("slug")
    wait.add_argument("--timeout", type=int, default=900)
    wait.add_argument("--all", action="store_true")
    wait.set_defaults(func=cmd_wait)

    comments = add("comments", help="read comments")
    comments.add_argument("slug")
    comments.add_argument("--open", action="store_true", help="only unresolved comments")
    comments.add_argument("--since", type=int, help="only comments with a larger sequence number")
    comments.set_defaults(func=cmd_comments)

    reply = add("reply", help="reply to a comment")
    reply.add_argument("comment_id")
    reply.add_argument("body")
    reply.set_defaults(func=cmd_reply)

    release = add("release", help="hand a claimed comment back to the queue")
    release.add_argument("comment_id")
    release.set_defaults(func=cmd_release)

    for name, status in (("resolve", "resolved"), ("reopen", "open")):
        command = add(name, help=f"mark a comment {status}")
        command.add_argument("comment_id")
        command.set_defaults(func=cmd_status, status=status)

    opener = add("open", help="open the review page in a browser")
    opener.add_argument("slug")
    opener.add_argument("--raw", action="store_true", help="open the artifact without the review panel")
    opener.set_defaults(func=cmd_open)

    remove = add("rm", help="delete an artifact and its comments")
    remove.add_argument("slug")
    remove.set_defaults(func=cmd_rm)

    whoami = add("whoami", help="show the handle and session this session resolves to")
    whoami.set_defaults(func=cmd_whoami)

    serve = add("serve", help="run the artifact server in the foreground")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
