"""Prepare a bounded, local agent-relay issue draft for human review."""
import argparse
import json
import re
import sys
import unicodedata
from urllib.parse import urlencode

REPOSITORY = "https://github.com/Ami-Khokhar/agent-relay/issues/new"
LIMIT = 1000
_CATEGORIES = {"crash", "incorrect_behavior", "unavailable", "other"}
_OPERATIONS = {"start", "delegate", "wait", "cancel", "workflow", "mcp", "other"}
_SYSTEMS = {"Linux", "macOS", "Windows", "Other"}
_VERSION = re.compile(r"(?:unknown|[0-9a-fA-F]{7,40}|v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)\Z")
_SECRET = re.compile(r"(?i)(?:\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
                     r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
                     r"\b[a-z][a-z0-9+.-]*://[^/@\s]+@|"
                     r"\b[A-Z0-9_-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|"
                     r"API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|AUTH[_-]?KEY)"
                     r"[A-Z0-9_-]*\b\s*[:=]\s*[^,;\s]+|"
                     r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                     r"sk-[A-Za-z0-9_-]{20,}|Bearer\s+\S+|xox[baprs]-[A-Za-z0-9-]{10,})")


def _field(value, name, choices=None, required=True, limit=LIMIT):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError("invalid " + name + ": provide text up to " + str(limit) + " characters")
    value = value.strip()
    if choices is not None and value not in choices:
        raise ValueError("invalid " + name + ": choose a supported value")
    return value


def _free_text(value, name, limit=LIMIT):
    value = _field(value, name, limit=limit)
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        raise ValueError("unsupported control or bidirectional character in " + name)
    if _SECRET.search(value):
        raise ValueError("draft contains a credential-like value; remove it before preparing a report")
    return value


def prepare_feedback(category, operation, system, version, blocked_action="", workaround=""):
    """Return a draft and canonical issue-form link; performs no I/O."""
    if (not isinstance(blocked_action, str) or not blocked_action.strip() or
            not isinstance(workaround, str) or not workaround.strip()):
        raise ValueError("a concrete blocked action and workaround actually used are required")
    category = _field(category, "category", _CATEGORIES)
    operation = _field(operation, "operation", _OPERATIONS)
    system = _field(system, "system", _SYSTEMS)
    version = _free_text(version, "version", limit=80)
    if not version.isascii() or not _VERSION.fullmatch(version):
        raise ValueError("version must be SemVer, a 7-40 character hex revision, or unknown")
    blocked_action = _free_text(blocked_action, "blocked action")
    workaround = _free_text(workaround, "workaround")
    title = "[Relay problem] " + category.replace("_", " ")
    body = ("## Error category\n" + category + "\n\n## Operation\n" + operation +
            "\n\n## Agent-relay version\n" + version + "\n\n## OS family\n" + system +
            "\n\n## Blocked action\n" + blocked_action +
            "\n\n## Workaround used\n" + workaround +
            "\n\nReview all public contents in GitHub before clicking Submit.\n")
    if len(body) > 4096:
        raise ValueError("draft is too long")
    form_url = REPOSITORY + "?" + urlencode({"title": title, "body": body})
    if len(form_url) > 2000:
        raise ValueError("description makes the prefilled URL too long; shorten it")
    return {"title": title, "body": body, "formUrl": form_url}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a local agent-relay feedback draft; never submits it.")
    parser.add_argument("--category", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--blocked-action", required=True)
    parser.add_argument("--workaround", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare_feedback(args.category, args.operation, args.system,
                                          args.version, args.blocked_action,
                                          args.workaround), ensure_ascii=False))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
