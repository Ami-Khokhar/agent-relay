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
`content-type: application/json`. For compatibility, an HTTP response without a JSON
content type is treated as raw success text when its status is 2xx.

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

List a credential's variable name in `inheritEnv` to pass it through:

```json
{ "id": "claude", "command": "claude", "args": ["--print"], "inheritEnv": ["ANTHROPIC_API_KEY"] }
```

Never inline secret values in the registry file.

## Cancellation

- `command` / `stdio`: the relay sends `SIGTERM`, then `SIGKILL` after ~1s.
- `http`: the relay aborts the request; work already accepted by the remote service may
  continue. Adapters should treat client disconnect as a cancel signal where possible.

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
