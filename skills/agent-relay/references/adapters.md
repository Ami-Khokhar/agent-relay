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
    "timeoutMs": 120000
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

Copy `examples/stdio-adapter.mjs` and replace `runHarness` with the harness's supported
API or CLI invocation:

```js
#!/usr/bin/env node

async function runHarness(input) {
  // Call the harness here and return its text result.
  // Example: spawn its CLI and capture stdout.
  return `example harness received: ${input}`
}

let raw = ''
for await (const chunk of process.stdin) raw += chunk

try {
  const request = JSON.parse(raw)
  if (request.protocolVersion !== 'relay.adapter/v1' || typeof request.task?.input !== 'string') {
    throw new Error('invalid relay.adapter/v1 request')
  }
  const output = await runHarness(request.task.input)
  process.stdout.write(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'completed', output }))
} catch (error) {
  process.stdout.write(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'failed', error: error.message }))
}
```

Register it:

```json
{ "id": "my-harness", "type": "stdio", "command": "node", "args": ["/abs/adapter.mjs"], "cwd": "/path/to/project" }
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
| Codex | `{ "command": "codex", "args": ["exec"] }` |
| Pi | `{ "command": "pi", "args": ["--print"] }` |
| OpenCode | `{ "command": "opencode", "args": ["run", "--dir", "/path", "--agent", "build"] }` |
| DeepSeek Harness | `{ "command": "dsh", "args": ["--profile", "headless"] }` |

A `command` adapter appends the task input as the **final argument** (no shell), so it only
works for CLIs that accept the prompt positionally. If the CLI needs a flag, a file, or
structured output, write a `stdio` wrapper instead. Command input defaults to 64 KiB
because it is one OS argument.

## Testing an adapter

Feed it a request by hand:

```bash
printf '%s\n' '{"protocolVersion":"relay.adapter/v1","task":{"id":"t","sessionId":"s","input":"hi","timeoutMs":1000}}' \
  | node /abs/adapter.mjs
```

It must print a single valid result document and nothing else on stdout. Then register it
and submit a task through the relay to confirm end to end.
