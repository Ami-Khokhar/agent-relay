#!/usr/bin/env node

// Replace this function with the supported API for the harness being adapted.
async function runHarness(input) {
  return `example harness received: ${input}`
}

let raw = ''
for await (const chunk of process.stdin) raw += chunk

try {
  const request = JSON.parse(raw)
  if (request.protocolVersion !== 'relay.adapter/v1' || typeof request.task?.input !== 'string') throw new Error('invalid relay.adapter/v1 request')
  const output = await runHarness(request.task.input)
  process.stdout.write(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'completed', output }))
} catch (error) {
  process.stdout.write(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'failed', error: error.message }))
}
