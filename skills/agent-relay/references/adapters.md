# Adapter contract and harness recipes

An adapter is how the relay reaches a target harness. There are three kinds, but `stdio`
and `http` share one versioned, harness-neutral contract: `relay.adapter/v1`.

## The contract

The relay sends exactly one request document:

```json
{
  "protocolVersion": "relay.adapter/v1",
  "task": {
    "id": "relay-task-id",
    "sessionId": "correlation-id",
    "input": "do the work",
    "timeoutMs": 900000
  }
}
```

The adapter returns exactly one result document:

```json
{"protocolVersion":"relay.adapter/v1","status":"completed","output":"result text"}
```

```json
{"protocolVersion":"relay.adapter/v1","status":"failed","error":"reason"}
```

Validation rules enforced by the relay (a violation fails the task):

- the top-level value is a non-array object;
- `protocolVersion` is exactly `relay.adapter/v1`;
- `status` is `completed` or `failed`;
- `output` and `error`, if present, are strings;
- a `failed` result must include a non-empty `error`.

A `stdio` adapter must write **only** the result to stdout and send diagnostics to stderr.
An `http` adapter accepts a `POST` and returns the result with
`content-type: application/json`. It receives the complete prompt, so use `https://` for a
remote adapter (cleartext `http://` to a non-loopback host needs `"allowInsecureHttp": true`
in its registry entry). The relay does not follow redirects: a `3xx` fails the task.
For compatibility, an HTTP response without a JSON content type is treated as raw success
text when its status is 2xx.

## Minimal stdio adapter template

Copy `examples/stdio_adapter.py` and replace `run_harness` with the harness's supported
API or CLI invocation:

```python
#!/usr/bin/env python3
import json
import sys


def run_harness(prompt):
    # Call the harness here and return its text result.
    # Example: run its CLI and capture stdout.
    return f"example harness received: {prompt}"


def main():
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
        task = request.get("task") if isinstance(request, dict) else None
        if request.get("protocolVersion") != "relay.adapter/v1" \
                or not isinstance(task, dict) or not isinstance(task.get("input"), str):
            raise ValueError("invalid relay.adapter/v1 request")
        output = run_harness(task["input"])
        result = {"protocolVersion": "relay.adapter/v1", "status": "completed", "output": output}
    except Exception as exc:
        result = {"protocolVersion": "relay.adapter/v1", "status": "failed", "error": str(exc)}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
```

Register it:

```json
{ "id": "my-harness", "type": "stdio", "command": "python3", "args": ["/abs/adapter.py"], "cwd": "/path/to/project" }
```

## Environment available to adapters

Command and stdio children receive only:

`PATH`, `HOME`, `USER`, `SHELL`, `TMPDIR`, `LANG`, `LC_ALL`, `SystemRoot`, `ComSpec`,
`PATHEXT` — plus anything in the agent's `inheritEnv` list, the agent's `env` map, and:

- `A2A_ADAPTER_PROTOCOL=relay.adapter/v1`
- `A2A_TASK_ID`, `A2A_SESSION_ID` (command adapters; stdio adapters get them in the request)
- `AGENT_RELAY_PARENT_TASK_ID` — the child's own task ID (command and stdio adapters); the
  MCP `delegate` tool forwards it as `parentTaskId` so the relay applies its delegation policy

List a credential's variable name in `inheritEnv` to pass it through:

```json
{ "id": "claude", "command": "claude", "args": ["--print"], "inheritEnv": ["ANTHROPIC_API_KEY"] }
```

Never inline secret values in the registry file.

## Cancellation

- `command` / `stdio`: the relay sends `SIGTERM`, then `SIGKILL` after ~1s, to the
  adapter's process group, so processes it started stop too (Windows: direct process only).
  After the adapter itself has exited and been reaped, the group is signalled again only where
  the leader's identity can be checked (Linux); elsewhere leftover descendants are not.
- `http`: `request_only`. The relay marks the task cancelled and discards the result; it does
  not abort the in-flight request or notify the service, so the work may continue.

On relay `SIGTERM`/`SIGINT`, running adapters are cancelled the same way before exit.

## Harness recipes

These are entry points, not a guarantee — verify flags against the installed version.

| Harness | Registry entry |
| --- | --- |
| Claude Code | `{ "command": "claude", "args": ["--print"] }` |
| Codex | `{ "command": "codex", "args": ["exec", "--skip-git-repo-check", "-s", "workspace-write"] }` |
| Pi | `{ "command": "pi", "args": ["--print"] }` |
| OpenCode | `{ "command": "opencode", "args": ["run", "--dir", "/path", "--agent", "build"] }` |
| DeepSeek Harness | `{ "command": "dsh", "args": ["--profile", "headless"] }` |

Codex: `--skip-git-repo-check` allows `codex exec` outside a git repository, and
`-s workspace-write` lifts the default read-only sandbox so a delegated fix can write
files (checked against codex-cli 0.155.1). Without the second flag, a task can succeed
while making no changes.

A task can choose among the per-task `options` the agent declares in its registry entry
(`"options": {"effort": {"high": ["-c", "model_reasoning_effort=high"]}}`, chosen with
`"options": {"effort": "high"}`). The mapped args are added after the entry's `args` and before
the task input, for `command` and `stdio` agents. A `stdio` adapter sees them as its own
arguments; the `relay.adapter/v1` request does not change. An `http` entry cannot declare options.

A task can ask for a read-only run with `"readOnly": true` when the agent's registry entry has a
`readOnly` profile (`{"args": ["--exclude-tools", "edit,write"], "sandbox": true}`). The profile
`args` follow the option args and precede the task input. A `command` profile may also set
`"inputSeparator": "--"`: the relay puts it after the profile `args` and before the input, so any
input is data. Without one, and when the profile has `args`, a read-only task whose input starts
with `-` is refused (`400 invalid_input`), because the input is the last argument and could be read
as an option that overrides the profile (`--access=read` then `--access=write`). A `stdio` entry
cannot declare a separator: its input is on stdin. With `"sandbox": true` on macOS the
relay starts the adapter under `/usr/bin/sandbox-exec` with a profile that denies file writes
under the task's `cwd`, the agent's `allowedRoots` and the git metadata of the `cwd` (a worktree's
own git directory and the shared one, found with `git rev-parse`; a bare repository's git
directory too; reads stay allowed), renaming or
deleting any directory above those trees (the ancestor rule), reading the
relay token file, and connecting to the relay's TCP port, and allows everything else (the relay also removes
`AGENT_RELAY_TOKEN` and `A2A_RELAY_TOKEN` from the adapter's environment). It does not stop a
hard link that already exists outside the tree, writes made for the adapter by another local
service such as a Docker socket, reads of other files, or other network access. macOS does not
nest sandboxes, so an adapter that applies its own sandbox fails under `"sandbox": true`; use
`args` for such an adapter. An `http` entry cannot declare a profile. The git lookup fails closed:
when a `.git` entry (or a directory with `HEAD`, `objects` and `refs`, as a bare repository has)
exists at or above the `cwd` and git cannot answer, the task fails with `read_only_git_unknown`
and the adapter does not start. The lookup runs outside the sandbox, so it runs `/usr/bin/git` (or
the owner's `AGENT_RELAY_GIT`) by absolute path, never through `PATH`, with only `PATH=/usr/bin:/bin`,
`HOME`, `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`: the global git config is not read,
so a repository that needs `safe.directory` there is refused. The relay checks a queued task against the
registry entry again when it dispatches it, so a reload that removed the profile fails the task
with `read_only_unsupported` instead of running it.

A task can override the working directory with `cwd` on `POST /v1/tasks` / MCP `delegate`.
The relay resolves the path and accepts it only when it is inside the agent's
`allowedRoots` (or equal to the agent's `cwd`); otherwise it returns `400 cwd_not_allowed`
and the adapter never runs.

A `command` adapter appends the task input as the **final argument** (no shell), so it only
works for CLIs that accept the prompt positionally. If the CLI needs a flag, a file, or
structured output, write a `stdio` wrapper instead. Command input defaults to 64 KiB
because it is one OS argument.

## Testing an adapter

Feed it a request by hand:

```bash
printf '%s\n' '{"protocolVersion":"relay.adapter/v1","task":{"id":"t","sessionId":"s","input":"hi","timeoutMs":1000}}' \
  | python3 /abs/adapter.py
```

It must print a single valid result document and nothing else on stdout. Then register it
and submit a task through the relay to confirm end to end.
