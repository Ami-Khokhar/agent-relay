#!/usr/bin/env python3
"""agent-artifacts: a local, reviewable artifact service for any coding agent.

An agent publishes an HTML file and gets a URL. The person opens it, comments on
elements, and can address a comment to one agent (``@codex``) or leave it for
everyone attached. Sessions attach to an artifact, survive their own process, and
are detached explicitly or by lease expiry. Delivery is pull based: an agent sees
its inbox while it is waiting, and comments addressed to an absent agent stay
queued until that agent comes back.

Zero dependencies. Storage lives in ``~/.local/state/agent-relay/artifacts``.
See docs/artifacts.md for the playbook agents follow.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
HANDLE_RE = re.compile(r"@[a-z0-9][a-z0-9._-]{0,39}")
MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9._-]{0,39})", re.IGNORECASE)
OWNER = "@owner"


def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return value
    return default


def _int_env(default, *names):
    raw = _env(*names)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"{names[0]} must be an integer")
    if value <= 0:
        sys.exit(f"{names[0]} must be a positive integer")
    return value


HOST = _env("AGENT_ARTIFACTS_HOST", default="127.0.0.1")
PORT = _int_env(43126, "AGENT_ARTIFACTS_PORT")
# Artifact HTML is untrusted. Serving it from a second loopback port gives it a
# different origin, so it cannot read the review page's DOM or call the API
# through window.parent the way same-origin HTML can.
CONTENT_PORT = _int_env(PORT + 1, "AGENT_ARTIFACTS_CONTENT_PORT")
MAX_HTML = _int_env(8 * 1024 * 1024, "AGENT_ARTIFACTS_MAX_BYTES")
MAX_BODY = _int_env(16 * 1024 * 1024, "AGENT_ARTIFACTS_MAX_BODY_BYTES")
MAX_WAIT = _int_env(600, "AGENT_ARTIFACTS_MAX_WAIT_SECONDS")
MAX_COMMENT = _int_env(20_000, "AGENT_ARTIFACTS_MAX_COMMENT_CHARS")
MAX_ARTIFACTS = _int_env(500, "AGENT_ARTIFACTS_MAX_COUNT")
LEASE_SECONDS = _int_env(30, "AGENT_ARTIFACTS_LEASE_SECONDS")
WEB_DIR = Path(__file__).resolve().parent / "artifacts_web"
LOCK = threading.RLock()
_PROCESS_LOCK_FD = None
_PROCESS_LOCK_DEPTH = 0


@contextlib.contextmanager
def _process_lock():
    """Advisory lock shared by every process on this state directory.

    Two servers pointed at one directory otherwise read, change and write the same
    artifact.json concurrently and silently drop each other's comments.
    """
    global _PROCESS_LOCK_FD, _PROCESS_LOCK_DEPTH
    if _PROCESS_LOCK_DEPTH == 0:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _PROCESS_LOCK_FD = open(directory / ".lock", "a+")
        fcntl.flock(_PROCESS_LOCK_FD, fcntl.LOCK_EX)
    _PROCESS_LOCK_DEPTH += 1
    try:
        yield
    finally:
        _PROCESS_LOCK_DEPTH -= 1
        if _PROCESS_LOCK_DEPTH == 0 and _PROCESS_LOCK_FD is not None:
            fcntl.flock(_PROCESS_LOCK_FD, fcntl.LOCK_UN)
            _PROCESS_LOCK_FD.close()
            _PROCESS_LOCK_FD = None


@contextlib.contextmanager
def guard():
    """In-process mutex plus the cross-process lock, in that order."""
    with LOCK:
        with _process_lock():
            yield
VERSION_HINT = "<!-- injected by agent-artifacts -->"
ARTIFACT_CSP = (
    "default-src 'self' data: blob: https: http:; script-src 'self'; connect-src 'none'; "
    "frame-ancestors {origin}")


def artifact_csp() -> str:
    return ARTIFACT_CSP.format(origin=public_base())


def state_dir() -> Path:
    raw = _env("AGENT_ARTIFACTS_DIR", "A2A_ARTIFACTS_DIR")
    if raw:
        return Path(raw).expanduser()
    base = _env("XDG_STATE_HOME", default=str(Path.home() / ".local" / "state"))
    return Path(base) / "agent-relay" / "artifacts"


def now_iso(timestamp=None) -> str:
    moment = datetime.fromtimestamp(timestamp, timezone.utc) if timestamp else datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def norm_handle(value, default=OWNER):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if not text.startswith("@"):
        text = "@" + text
    if not HANDLE_RE.fullmatch(text):
        raise ValueError(f"invalid handle: {value!r} (use @name with a-z, 0-9, dot, dash)")
    return text


def mentions(text) -> list:
    seen = []
    for match in MENTION_RE.finditer(str(text or "")):
        handle = "@" + match.group(1).lower()
        if handle != OWNER and handle not in seen and HANDLE_RE.fullmatch(handle):
            seen.append(handle)
    return seen


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:48] or "artifact"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def artifact_path(slug: str) -> Path:
    return state_dir() / slug / "artifact.json"


def normalize(artifact):
    """Fill fields introduced after an artifact was first written."""
    artifact.setdefault("origin", None)
    artifact.setdefault("sessions", [])
    artifact.setdefault("presence", {})
    artifact.setdefault("comments", [])
    artifact.setdefault("nextSeq", 1 + max([c.get("seq", 0) for c in artifact["comments"]] or [0]))
    for comment in artifact["comments"]:
        comment.setdefault("to", [])
        comment.setdefault("claims", {})
        comment.setdefault("replies", [])
    return artifact


def load(slug: str):
    if not SLUG_RE.fullmatch(slug or ""):
        return None
    artifact = read_json(artifact_path(slug))
    if not isinstance(artifact, dict) or artifact.get("slug") != slug:
        return None
    return normalize(artifact)


def save(artifact) -> None:
    artifact["updated"] = now_iso()
    write_json(artifact_path(artifact["slug"]), artifact)


def all_artifacts():
    root = state_dir()
    if not root.is_dir():
        return []
    items = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        data = read_json(child / "artifact.json")
        if isinstance(data, dict) and data.get("slug") == child.name:
            items.append(normalize(data))
    items.sort(key=lambda item: item.get("updated") or "", reverse=True)
    return items


def unique_slug(base: str, current=None):
    taken = {item["slug"] for item in all_artifacts() if item["slug"] != current}
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def version_file(slug: str, number: int) -> Path:
    return state_dir() / slug / "versions" / f"{number}.html"


def read_version(slug: str, number: int):
    artifact = load(slug)
    if not artifact:
        return None
    if number not in [item["n"] for item in artifact["versions"]]:
        return None
    try:
        return version_file(slug, number).read_text(encoding="utf-8")
    except OSError:
        return None


def current_version(artifact) -> int:
    return max(item["n"] for item in artifact["versions"])


def public_base() -> str:
    host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    return _env("AGENT_ARTIFACTS_PUBLIC_URL", default=f"http://{host}:{PORT}")


def content_base() -> str:
    host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    return _env("AGENT_ARTIFACTS_CONTENT_URL", default=f"http://{host}:{CONTENT_PORT}")


def publish(html: str, title=None, slug=None, note=None, handle=None, session=None, cwd=None):
    if not isinstance(html, str) or not html.strip():
        raise ValueError("html is required")
    data = html.encode("utf-8")
    if len(data) > MAX_HTML:
        raise ValueError(f"artifact is larger than {MAX_HTML} bytes")
    with guard():
        artifact = load(slug) if slug else None
        if slug and not SLUG_RE.fullmatch(slug):
            raise ValueError("slug must match [a-z0-9][a-z0-9-]*")
        if artifact is None:
            if len(all_artifacts()) >= MAX_ARTIFACTS:
                raise ValueError(f"artifact limit reached ({MAX_ARTIFACTS})")
            name = slug or unique_slug(slugify(title or "artifact"))
            artifact = normalize({
                "slug": name,
                "title": title or name,
                "created": now_iso(),
                "updated": now_iso(),
                "versions": [],
            })
        number = (current_version(artifact) if artifact["versions"] else 0) + 1
        target = version_file(artifact["slug"], number)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        if title:
            artifact["title"] = title
        artifact["versions"].append({
            "n": number,
            "created": now_iso(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "note": note,
            "by": norm_handle(handle) if handle else None,
        })
        if handle:
            record = attach(artifact, handle, session=session, cwd=cwd, role="origin")
            if artifact["origin"] is None or number == 1:
                artifact["origin"] = {"handle": record["handle"], "session": record["id"],
                                      "cwd": cwd, "at": now_iso()}
        save(artifact)
        return {
            "slug": artifact["slug"],
            "title": artifact["title"],
            "version": number,
            "versions": len(artifact["versions"]),
            "url": f"{public_base()}/a/{artifact['slug']}",
        "rawBase": f"{content_base()}/a/{artifact['slug']}",
            "raw": f"{content_base()}/a/{artifact['slug']}/raw?v={number}",
            "bytes": len(data),
            "sha256": artifact["versions"][-1]["sha256"],
            "session": (attach_find(artifact, handle, session) or {}).get("id") if handle else None,
            "origin": artifact.get("origin"),
        }


def attach_find(artifact, handle, session):
    if not handle:
        return None
    handle = norm_handle(handle)
    for record in artifact["sessions"]:
        if session and record["id"] == session:
            return record
        if record["handle"] == handle and record["role"] == "origin":
            return record
    for record in artifact["sessions"]:
        if record["handle"] == handle:
            return record
    return None


def attach(artifact, handle, session=None, cwd=None, role="attached"):
    """Attach a session to an artifact. Idempotent for a handle without a session."""
    handle = norm_handle(handle)
    explicit = (session or "").strip()
    if explicit:
        for record in artifact["sessions"]:
            if record["id"] == explicit:
                if record["handle"] != handle:
                    raise ValueError(
                        f"session {explicit} is already attached as {record['handle']}")
                record["handle"] = handle
                record["lastSeen"] = now_iso()
                if cwd:
                    record["cwd"] = cwd
                return record
        record = {"id": explicit, "handle": handle, "cwd": cwd, "role": role,
                  "attachedAt": now_iso(), "lastSeen": now_iso()}
        artifact["sessions"].append(record)
        return record
    for wanted in ("origin", "attached"):
        for record in artifact["sessions"]:
            if record["handle"] == handle and record["role"] == wanted:
                record["lastSeen"] = now_iso()
                if cwd:
                    record["cwd"] = cwd
                return record
    record = {"id": "s-" + secrets.token_hex(4), "handle": handle, "cwd": cwd, "role": role,
              "attachedAt": now_iso(), "lastSeen": now_iso()}
    artifact["sessions"].append(record)
    return record


def detach(artifact, *, handle=None, session=None):
    removed = []
    keep = []
    target_handle = norm_handle(handle) if handle else None
    for record in artifact["sessions"]:
        if (session and record["id"] == session) or (target_handle and record["handle"] == target_handle):
            removed.append(record)
            artifact["presence"].pop(record["handle"], None)
        else:
            keep.append(record)
    if not removed:
        raise KeyError(handle or session or "session")
    artifact["sessions"] = keep
    return removed


def heartbeat(artifact, handle, session=None, lease=None):
    handle = norm_handle(handle)
    record = attach_find(artifact, handle, session)
    if record is None:
        record = attach(artifact, handle, session=session)
    else:
        record["lastSeen"] = now_iso()
    seconds = LEASE_SECONDS if lease is None else max(2, min(int(lease), 300))
    until = time.time() + seconds
    artifact["presence"][handle] = {"session": record["id"], "leaseUntil": until,
                                    "lastSeen": now_iso(), "seconds": seconds}
    return artifact["presence"][handle]


def is_live(artifact, handle) -> bool:
    entry = artifact["presence"].get(handle)
    return bool(entry) and entry.get("leaseUntil", 0) > time.time()


def artifact_handles(artifact) -> list:
    handles = []
    for record in artifact["sessions"]:
        if record["handle"] not in handles:
            handles.append(record["handle"])
    return handles


def add_comment(slug, body, author="owner", version=None, anchor=None, to=None):
    body = (body or "").strip()
    if not body:
        raise ValueError("body is required")
    if len(body) > MAX_COMMENT:
        raise ValueError(f"comment is longer than {MAX_COMMENT} characters")
    with guard():
        artifact = load(slug)
        if artifact is None:
            raise KeyError(slug)
        number = version if isinstance(version, int) and version > 0 else current_version(artifact)
        if number not in [item["n"] for item in artifact["versions"]]:
            number = current_version(artifact)
        clean_anchor = None
        if isinstance(anchor, dict):
            clean_anchor = {
                "selector": str(anchor.get("selector") or "")[:600],
                "quote": str(anchor.get("quote") or "")[:300],
                "x": anchor.get("x") if isinstance(anchor.get("x"), (int, float)) else 0.5,
                "y": anchor.get("y") if isinstance(anchor.get("y"), (int, float)) else 0.5,
            }
        targets = []
        for value in (to or []):
            handle = norm_handle(value)
            if handle != OWNER and handle not in targets:
                targets.append(handle)
        for handle in mentions(body):
            if handle not in targets:
                targets.append(handle)
        if not targets:
            # Unaddressed feedback belongs to the agent that published it; anyone
            # else gets it by being named, or by asking for everything.
            handles = artifact_handles(artifact)
            origin_handle = (artifact.get("origin") or {}).get("handle")
            targets = [origin_handle] if origin_handle else handles
        comment = {
            "id": "c" + secrets.token_hex(3),
            "seq": artifact.get("nextSeq", 1),
            "version": number,
            "author": norm_handle(author, default=OWNER),
            "body": body,
            "anchor": clean_anchor,
            "status": "open",
            "created": now_iso(),
            "to": targets,
            "claims": {},
            "replies": [],
        }
        artifact["nextSeq"] = comment["seq"] + 1
        artifact["comments"].append(comment)
        save(artifact)
        return with_delivery(artifact, comment)


def with_delivery(artifact, comment):
    view = dict(comment)
    delivery = []
    claimed = comment.get("claims") or {}
    if not (comment.get("to") or []):
        for handle, claim in claimed.items():
            delivery.append({"handle": handle, "state": "claimed", "live": is_live(artifact, handle),
                             "session": claim.get("session"), "at": claim.get("at")})
        view["delivery"] = delivery
        return view
    for handle in comment.get("to") or []:
        claim = (comment.get("claims") or {}).get(handle)
        if comment.get("status") == "resolved":
            state = "resolved"
        elif claim:
            state = "claimed"
        elif is_live(artifact, handle):
            state = "queued"
        else:
            state = "waiting"
        delivery.append({"handle": handle, "state": state, "live": is_live(artifact, handle),
                         "session": (claim or {}).get("session"), "at": (claim or {}).get("at")})
    view["delivery"] = delivery
    return view


def find_comment(artifact, comment_id):
    for comment in artifact["comments"]:
        if comment.get("id") == comment_id:
            return comment
    return None


def find_artifact_for_comment(comment_id):
    for artifact in all_artifacts():
        comment = find_comment(artifact, comment_id)
        if comment is not None:
            return artifact, comment
    return None, None


def reply(comment_id, body, author="agent"):
    body = (body or "").strip()
    if not body:
        raise ValueError("body is required")
    if len(body) > MAX_COMMENT:
        raise ValueError(f"reply is longer than {MAX_COMMENT} characters")
    with guard():
        artifact, comment = find_artifact_for_comment(comment_id)
        if comment is None:
            raise KeyError(comment_id)
        item = {"id": "r" + secrets.token_hex(3), "author": norm_handle(author, default="@agent"),
                "body": body, "created": now_iso()}
        comment.setdefault("replies", []).append(item)
        save(artifact)
        return item


def release(comment_id, handle):
    """Hand a claimed comment back so another agent can pick it up."""
    handle = norm_handle(handle)
    with guard():
        artifact, comment = find_artifact_for_comment(comment_id)
        if comment is None:
            raise KeyError(comment_id)
        if handle not in (comment.get("claims") or {}):
            raise KeyError(handle)
        comment["claims"].pop(handle, None)
        save(artifact)
        return with_delivery(artifact, comment)


def set_status(comment_id, status, author="owner"):
    if status not in ("open", "resolved"):
        raise ValueError("status must be open or resolved")
    with guard():
        artifact, comment = find_artifact_for_comment(comment_id)
        if comment is None:
            raise KeyError(comment_id)
        comment["status"] = status
        comment["resolvedBy"] = norm_handle(author, default=OWNER) if status == "resolved" else None
        comment["resolvedAt"] = now_iso() if status == "resolved" else None
        save(artifact)
        return with_delivery(artifact, comment)


def list_comments(slug, since=None, open_only=False):
    artifact = load(slug)
    if artifact is None:
        raise KeyError(slug)
    comments = artifact["comments"]
    if isinstance(since, int):
        comments = [item for item in comments if item.get("seq", 0) > since]
    if open_only:
        comments = [item for item in comments if item.get("status") != "resolved"]
    return [with_delivery(artifact, comment) for comment in comments]


def inbox(slug, handle, session=None, wait=0, lease=None, include_all=False):
    """Claim and return comments addressed to ``handle``; long-poll while empty.

    ``wait`` also renews the caller's presence, which is what makes an agent
    "active" for mention routing.
    """
    handle = norm_handle(handle)
    deadline = time.time() + max(0, min(int(wait or 0), MAX_WAIT))
    renewed = 0.0
    while True:
        with guard():
            artifact = load(slug)
            if artifact is None:
                raise KeyError(slug)
            if time.time() - renewed > 2 or renewed == 0.0:
                heartbeat(artifact, handle, session=session, lease=lease)
                save(artifact)
                renewed = time.time()
            claimed = []
            for comment in artifact["comments"]:
                if comment.get("status") == "resolved":
                    continue
                targets = comment.get("to") or []
                addressed = handle in targets
                # An unaddressed comment belongs to whoever is attached, so the
                # first agent to read its inbox claims it.
                broadcast = not targets and not (comment.get("claims") or {})
                others = include_all and bool(targets) and not addressed
                if not (addressed or broadcast or others):
                    continue
                if handle in (comment.get("claims") or {}):
                    continue
                comment.setdefault("claims", {})[handle] = {
                    "session": (attach_find(artifact, handle, session) or {}).get("id"),
                    "at": now_iso(),
                }
                claimed.append(with_delivery(artifact, comment))
            if claimed:
                save(artifact)
                return {"slug": slug, "handle": handle, "comments": claimed,
                        "session": (attach_find(artifact, handle, session) or {}).get("id"),
                        "presence": artifact["presence"].get(handle)}
        if time.time() >= deadline:
            return {"slug": slug, "handle": handle, "comments": [],
                    "session": (attach_find(artifact, handle, session) or {}).get("id"),
                    "presence": None}
        time.sleep(0.25)


def delete_artifact(slug):
    with guard():
        artifact = load(slug)
        if artifact is None:
            raise KeyError(slug)
        root = state_dir() / slug
        for child in sorted(root.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
        for child in sorted(root.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
        root.rmdir()
        return True


def artifact_summary(artifact):
    comments = artifact.get("comments") or []
    handles = artifact_handles(artifact)
    return {
        "slug": artifact["slug"],
        "title": artifact["title"],
        "created": artifact["created"],
        "updated": artifact["updated"],
        "currentVersion": current_version(artifact),
        "versions": artifact["versions"],
        "openComments": sum(1 for item in comments if item.get("status") != "resolved"),
        "totalComments": len(comments),
        "url": f"{public_base()}/a/{artifact['slug']}",
        "rawBase": f"{content_base()}/a/{artifact['slug']}",
        "origin": artifact.get("origin"),
        "handles": handles,
        "sessions": session_view(artifact),
    }


def session_view(artifact):
    now = time.time()
    out = []
    for record in artifact["sessions"]:
        entry = artifact["presence"].get(record["handle"]) or {}
        live = entry.get("leaseUntil", 0) > now and entry.get("session") == record["id"]
        out.append({**record, "live": live,
                    "leaseUntil": entry.get("leaseUntil"),
                    "leaseLeft": max(0, int(entry.get("leaseUntil", 0) - now)) if live else 0})
    return out


def inject_overlay(html: str, slug: str, version: int) -> str:
    # Config travels on the tag, not in an inline script, so the page needs no
    # 'unsafe-inline' and the artifact keeps a strict CSP.
    snippet = (
        f'\n{VERSION_HINT}\n<script src="/_artifacts/overlay.js" '
        f'data-slug="{escape(slug)}" data-version="{int(version)}" '
        f'data-parent="{escape(public_base())}"></script>\n'
    )
    match = re.search(r"</body\s*>", html, re.IGNORECASE)
    if match:
        return html[: match.start()] + snippet + html[match.start():]
    return html + snippet


INDEX_SCRIPT = """<script>
 document.querySelectorAll("time[datetime]").forEach(function (node) {
   var when = new Date(node.getAttribute("datetime"));
   if (!isNaN(when)) node.textContent = when.toLocaleString();
 });
</script>"""


def index_page(artifacts) -> str:
    rows = []
    for item in artifacts:
        handles = " ".join(
            f'<span class="chip{" live" if any(s["handle"] == h and s["live"] for s in session_view(item)) else ""}">{escape(h)}</span>'
            for h in artifact_handles(item))
        rows.append(
            "<tr><td><a href=\"/a/{slug}\">{title}</a><div class=\"chips\">{handles}</div></td>"
            "<td class=\"num\">v{n}</td><td class=\"num\">{open}</td>"
            "<td class=\"muted\"><time datetime=\"{updated}\">{updated}</time></td></tr>".format(
                slug=item["slug"], title=escape(item["title"]), handles=handles or "",
                n=current_version(item), open=sum(
                    1 for c in item.get("comments") or [] if c.get("status") != "resolved"),
                updated=escape(item["updated"]),
            )
        )
    body = "".join(rows) or '<tr><td colspan="4" class="muted">No artifacts yet.</td></tr>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Artifacts</title>
<style>
 :root{{--bg:#f7f7f4;--fg:#161816;--muted:#5f655d;--line:#d8dbd5;--card:#fff;--accent:#2a5db0}}
 @media (prefers-color-scheme:dark){{:root{{--bg:#0f1110;--fg:#e9ebe5;--muted:#98a094;
   --line:#2a2e28;--card:#171a16;--accent:#84aaee}}}}
 body{{margin:0;padding:28px 16px;background:var(--bg);color:var(--fg);
   font:15px/1.5 ui-serif,Georgia,serif}}
 .wrap{{max-width:900px;margin:0 auto}}
 h1{{font-size:1.4rem;margin:0 0 4px}}
 p.sub{{color:var(--muted);font-size:.85rem;margin:0 0 22px}}
 table{{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);
   border-radius:8px;overflow:hidden;font:.9rem/1.4 ui-sans-serif,system-ui,sans-serif}}
 th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}}
 th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
 tr:last-child td{{border-bottom:none}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 .muted{{color:var(--muted)}}
 td.muted{{white-space:nowrap}}
 .chips{{margin-top:3px}}
 .chip{{display:inline-block;font-size:.7rem;border:1px solid var(--line);border-radius:999px;
   padding:0 6px;margin-right:4px;color:var(--muted)}}
 .chip.live{{color:var(--accent);border-color:var(--accent)}}
 a{{color:var(--accent)}}
</style></head><body><div class="wrap">
<h1>Artifacts</h1>
<p class="sub">Local, reviewable pages published by coding agents. Attach a session, comment on
elements, mention an agent with @name.</p>
<table><thead><tr><th>Artifact</th><th>Version</th><th>Open</th><th>Updated</th></tr></thead>
<tbody>{body}</tbody></table></div>
{INDEX_SCRIPT}</body></html>"""


def escape(value) -> str:
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-artifacts"

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value), "application/json")

    def _error(self, status, code, message=None):
        payload = {"error": code}
        if message:
            payload["message"] = message
        self._json(status, payload)

    def _body(self):
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            raise ValueError("body too large")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("body must be JSON")
        if not isinstance(value, dict):
            raise ValueError("body must be a JSON object")
        return value

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        try:
            if method in ("POST", "PUT", "PATCH", "DELETE") and \
                    self.headers.get("x-artifacts-client") != "1":
                # A cross-origin <img>, <form> or fetch cannot set this header
                # without a preflight we never grant, so an artifact page cannot
                # drive the API from inside the review frame.
                return self._error(400, "missing_client_header",
                                   "state changing calls need the x-artifacts-client header")

            parsed = urlsplit(self.path)
            path = parsed.path
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            if path == "/healthz":
                return self._json(200, {"ok": True, "artifacts": len(all_artifacts()),
                                        "dir": str(state_dir()),
                                        "leaseSeconds": LEASE_SECONDS})
            if path == "/":
                return self._send(200, index_page(all_artifacts()), "text/html; charset=utf-8")
            if path == "/_artifacts/overlay.js":
                return self._web("overlay.js", "application/javascript; charset=utf-8")
            if path == "/api/artifacts":
                if method == "GET":
                    return self._json(200, {"artifacts": [artifact_summary(a) for a in all_artifacts()]})
                if method == "POST":
                    payload = self._body()
                    try:
                        result = publish(payload.get("html"), title=payload.get("title"),
                                         slug=payload.get("slug"), note=payload.get("note"),
                                         handle=payload.get("as") or payload.get("handle"),
                                         session=payload.get("session"), cwd=payload.get("cwd"))
                    except ValueError as exc:
                        return self._error(400, "invalid_publish", str(exc))
                    return self._json(201, result)
                return self._error(405, "method_not_allowed")
            match = re.fullmatch(r"/api/artifacts/([^/]+)", path)
            if match:
                slug = match.group(1)
                artifact = load(slug)
                if artifact is None:
                    return self._error(404, "not_found")
                if method == "GET":
                    data = artifact_summary(artifact)
                    data["comments"] = [with_delivery(artifact, c) for c in artifact["comments"]]
                    return self._json(200, data)
                if method == "DELETE":
                    delete_artifact(slug)
                    return self._json(200, {"deleted": slug})
                return self._error(405, "method_not_allowed")
            match = re.fullmatch(r"/api/artifacts/([^/]+)/comments", path)
            if match:
                slug = match.group(1)
                if load(slug) is None:
                    return self._error(404, "not_found")
                if method == "GET":
                    return self._comments(slug, query)
                if method == "POST":
                    payload = self._body()
                    try:
                        comment = add_comment(slug, payload.get("body"),
                                              author=payload.get("author") or "owner",
                                              version=payload.get("version"),
                                              anchor=payload.get("anchor"),
                                              to=payload.get("to"))
                    except KeyError:
                        return self._error(404, "not_found")
                    except ValueError as exc:
                        return self._error(400, "invalid_comment", str(exc))
                    return self._json(201, comment)
                return self._error(405, "method_not_allowed")
            match = re.fullmatch(r"/api/artifacts/([^/]+)/sessions", path)
            if match:
                return self._sessions(match.group(1), method)
            match = re.fullmatch(r"/api/artifacts/([^/]+)/sessions/([^/]+)", path)
            if match:
                return self._detach(match.group(1), match.group(2), method)
            match = re.fullmatch(r"/api/artifacts/([^/]+)/inbox", path)
            if match:
                if method != "POST":
                    return self._error(405, "method_not_allowed",
                                       "claiming is a POST; a GET would let any "
                                       "subresource claim a comment")
                payload = self._body()
                query = {key: str(value) for key, value in payload.items() if value is not None}
                return self._inbox(match.group(1), "GET", query)
            match = re.fullmatch(r"/api/comments/([^/]+)/(reply|status|release)", path)
            if match:
                if method != "POST":
                    return self._error(405, "method_not_allowed")
                comment_id, action = match.group(1), match.group(2)
                payload = self._body()
                try:
                    if action == "reply":
                        value = reply(comment_id, payload.get("body"),
                                      author=payload.get("author") or "agent")
                    elif action == "release":
                        value = release(comment_id, payload.get("author") or payload.get("as"))
                    else:
                        value = set_status(comment_id, payload.get("status"),
                                           author=payload.get("author") or "owner")
                except KeyError:
                    return self._error(404, "not_found")
                except ValueError as exc:
                    return self._error(400, "invalid_request", str(exc))
                return self._json(200, value)
            match = re.fullmatch(r"/a/([^/]+)(/raw)?", path)
            if match:
                slug, raw = match.group(1), bool(match.group(2))
                artifact = load(slug)
                if artifact is None:
                    return self._error(404, "not_found")
                if not raw:
                    return self._view(slug)
                return self._error(404, "not_found")
            return self._error(404, "not_found")
        except ValueError as exc:
            return self._error(400, "invalid_request", str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace
            self._error(500, "request_failed", str(exc))

    def _sessions(self, slug, method):
        if method == "GET":
            artifact = load(slug)
            if artifact is None:
                return self._error(404, "not_found")
            return self._json(200, {"slug": slug, "sessions": session_view(artifact),
                                    "handles": artifact_handles(artifact),
                                    "origin": artifact.get("origin")})
        if method == "POST":
            payload = self._body()
            with guard():
                artifact = load(slug)
                if artifact is None:
                    return self._error(404, "not_found")
                try:
                    record = attach(artifact, payload.get("as") or payload.get("handle"),
                                    session=payload.get("session"), cwd=payload.get("cwd"),
                                    role=payload.get("role") or "attached")
                except ValueError as exc:
                    return self._error(400, "invalid_attach", str(exc))
                save(artifact)
                return self._json(201, {"session": record, "sessions": session_view(artifact),
                                        "url": f"{public_base()}/a/{slug}"})
        return self._error(405, "method_not_allowed")

    def _detach(self, slug, session_id, method):
        if method != "DELETE":
            return self._error(405, "method_not_allowed")
        with guard():
            artifact = load(slug)
            if artifact is None:
                return self._error(404, "not_found")
            try:
                removed = detach(artifact, session=session_id)
            except KeyError:
                return self._error(404, "not_found")
            save(artifact)
            return self._json(200, {"detached": removed, "sessions": session_view(artifact)})

    def _inbox(self, slug, method, query):
        handle = query.get("as") or query.get("handle")
        if not handle:
            return self._error(400, "missing_handle")
        try:
            wait = max(0, int(query.get("wait") or 0))
        except ValueError:
            return self._error(400, "invalid_wait")
        try:
            result = inbox(slug, handle, session=query.get("session"), wait=wait,
                           lease=int(query["lease"]) if query.get("lease") else None,
                           include_all=str(query.get("all", "")).lower() in ("1", "true"))
        except KeyError:
            return self._error(404, "not_found")
        except ValueError as exc:
            return self._error(400, "invalid_inbox", str(exc))
        return self._json(200, result)

    def _comments(self, slug, query):
        since = query.get("since")
        try:
            since_value = int(since) if since not in (None, "") else None
        except ValueError:
            return self._error(400, "invalid_since")
        open_only = query.get("open") in ("1", "true")
        wait = query.get("wait")
        try:
            wait_value = max(0, int(wait)) if wait not in (None, "") else 0
        except ValueError:
            return self._error(400, "invalid_wait")
        artifact = load(slug)
        if artifact is None:
            return self._error(404, "not_found")
        if wait_value:
            # Read-only wait: no handle, no claim. Claiming is POST /inbox only,
            # so a subresource GET from an artifact cannot consume a comment.
            deadline = time.time() + min(wait_value, MAX_WAIT)
            while True:
                comments = list_comments(slug, since=since_value, open_only=open_only)
                if comments or time.time() >= deadline:
                    return self._json(200, {"slug": slug, "comments": comments,
                                            "count": len(comments)})
                time.sleep(0.25)
        comments = list_comments(slug, since=since_value, open_only=open_only)
        return self._json(200, {"slug": slug, "comments": comments, "count": len(comments)})

    def _view(self, slug):
        try:
            html = (WEB_DIR / "view.html").read_text(encoding="utf-8")
        except OSError:
            return self._error(500, "view_missing")
        return self._send(200, html, "text/html; charset=utf-8")

    def _web(self, name, content_type):
        path = WEB_DIR / name
        try:
            data = path.read_bytes()
        except OSError:
            return self._error(404, "not_found")
        return self._send(200, data, content_type)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


class ContentHandler(BaseHTTPRequestHandler):
    """Serves artifact HTML from its own origin, and nothing else."""

    protocol_version = "HTTP/1.1"
    server_version = "agent-artifacts-content"

    log_message = Handler.log_message
    _send = Handler._send
    _json = Handler._json
    _error = Handler._error

    def do_GET(self):
        try:
            self._get()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - answer instead of dropping the socket
            self._error(500, "request_failed", str(exc))

    def _get(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain; charset=utf-8")
        if path == "/_artifacts/overlay.js":
            try:
                data = (WEB_DIR / "overlay.js").read_bytes()
            except OSError:
                return self._error(404, "not_found")
            return self._send(200, data, "application/javascript; charset=utf-8")
        match = re.fullmatch(r"/a/([^/]+)/raw", path)
        if not match:
            return self._error(404, "not_found")
        slug = match.group(1)
        artifact = load(slug)
        if artifact is None:
            return self._error(404, "not_found")
        try:
            number = int(query.get("v") or 0) or current_version(artifact)
        except ValueError:
            return self._error(400, "invalid_version")
        html = read_version(slug, number)
        if html is None:
            return self._error(404, "not_found")
        if query.get("review") == "1":
            html = inject_overlay(html, slug, number)
        return self._send(200, html, "text/html; charset=utf-8",
                          {"content-security-policy": artifact_csp()})


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if HOST not in LOOPBACK and _env("AGENT_ARTIFACTS_ALLOW_REMOTE") != "1":
        sys.exit(
            f"agent-artifacts refuses to bind {HOST}: it is a local-only service with no\n"
            "authentication. Set AGENT_ARTIFACTS_ALLOW_REMOTE=1 only if you have put an\n"
            "authenticated proxy in front of it.")
    state_dir().mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    content = ThreadingHTTPServer((HOST, CONTENT_PORT), ContentHandler)
    content.daemon_threads = True
    threading.Thread(target=content.serve_forever, daemon=True).start()
    print(f"agent-artifacts listening on http://{HOST}:{PORT} "
          f"(artifacts on http://{HOST}:{CONTENT_PORT}, state {state_dir()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        content.shutdown()
        content.server_close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
