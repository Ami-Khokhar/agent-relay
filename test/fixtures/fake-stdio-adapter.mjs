if (process.env.FAKE_ADAPTER_MODE === 'early-exit') process.exit(0)
let raw = ''
for await (const chunk of process.stdin) raw += chunk
const request = JSON.parse(raw)
const mode = process.env.FAKE_ADAPTER_MODE
if (mode === 'malformed') process.stdout.write('not json')
else if (mode === 'failed') process.stdout.write(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'failed', error: 'fake failure' }))
else if (mode === 'oversized') process.stdout.write(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'completed', output: 'x'.repeat(10000) }))
else process.stdout.write(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'completed', output: `${request.task.sessionId}:${request.task.input}` }))
