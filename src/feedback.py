"""Prepare a bounded, local agent-relay issue draft for human review."""
import argparse
import json
import re
import sys
from urllib.parse import urlencode

REPOSITORY = "https://github.com/Ami-Khokhar/agent-relay/issues/new"
LIMIT = 1000
_CATEGORIES = {"crash", "incorrect_behavior", "unavailable", "other"}
_OPERATIONS = {"start", "delegate", "wait", "cancel", "workflow", "mcp", "other"}
_SYSTEMS = {"Linux", "macOS", "Windows", "Other"}
_SECRET = re.compile(r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                     r"sk-[A-Za-z0-9_-]{20,}|Bearer\s+\S+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)")


def _field(value, name, choices=None, required=True):
    if not isinstance(value, str) or len(value) > LIMIT or (required and not value.strip()):
        raise ValueError("invalid " + name + ": provide text up to 1000 characters")
    value = value.strip()
    if choices is not None and value not in choices:
        raise ValueError("invalid " + name + ": choose a supported value")
    return value


def prepare_feedback(category, operation, system, version, blocked_action="", workaround=""):
    """Return a draft and canonical issue-form link; performs no I/O."""
    if (not isinstance(blocked_action, str) or not blocked_action.strip() or
            not isinstance(workaround, str) or not workaround.strip()):
        raise ValueError("a concrete blocked action and workaround actually used are required")
    category = _field(category, "category", _CATEGORIES)
    operation = _field(operation, "operation", _OPERATIONS)
    system = _field(system, "system", _SYSTEMS)
    version = _field(version, "version")
    blocked_action = _field(blocked_action, "blocked action", required=True)
    workaround = _field(workaround, "workaround", required=True)
    if any(_SECRET.search(value) for value in (version, blocked_action, workaround)):
        raise ValueError("draft contains a credential-like value; remove it before preparing a report")
    if any(ord(char) < 32 and char not in "\n\t" for value in (blocked_action, workaround)
           for char in value):
        raise ValueError("evidence contains unsupported control characters")
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
