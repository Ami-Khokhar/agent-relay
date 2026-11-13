#!/usr/bin/env python3
"""agent-relay command-line entry point.

Installed on PATH by scripts/install-service.sh (the wrapper execs this file
with the checkout's python). Each subcommand answers a question a person asks
after installing the relay; the HTTP API itself stays in src/server.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_discovery  # noqa: E402
import server  # noqa: E402

CHECKOUT = Path(__file__).resolve().parent.parent


def _relay_url():
    # The installer's wrapper sets AGENT_RELAY_PORT, so this names the port the
    # service was installed with; an explicit relay URL still wins.
    explicit = server._env("AGENT_RELAY_URL", "A2A_RELAY_URL")
    if explicit:
        return explicit.rstrip("/")
    host = server.HOST if server.HOST != "0.0.0.0" else "127.0.0.1"
    return f"http://{host}:{server.PORT}"


def _print_info():
    print(f"relay: {_relay_url()}")
    print(f"artifact: {release_discovery.RELEASES_PAGE_URL}")
    print(f"token: {server.token_path()}")
    print(f"checkout: {CHECKOUT}")


def main(argv):
    commands = {"info": _print_info}
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip().splitlines()[0])
        print("usage: agent-relay <command>\ncommands: " + ", ".join(sorted(commands)))
        return 0
    command = commands.get(argv[0])
    if command is None:
        print(f"error: unknown command: {argv[0]}\n"
              f"commands: {', '.join(sorted(commands))}", file=sys.stderr)
        return 2
    command()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
