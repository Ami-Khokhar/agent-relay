import test from 'node:test'
import assert from 'node:assert/strict'
import { PassThrough } from 'node:stream'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { createHttpHandler, serve } from '../src/mcp-server.mjs'

test('maps MCP tools onto the HTTP task lifecycle', async () => {
  const calls = []
  const fakeFetch = async (url, options = {}) => {
    calls.push({ url, options })
    if (url.endsWith('/v1/agents')) return Response.json({ agents: [{ id: 'fake', adapter: 'command' }] })
    if (url.endsWith('/v1/tasks') && options.method === 'POST') return Response.json({ id: 'task-1', sessionId: 'session-1', agentId: 'fake', status: 'queued' }, { status: 202 })
    if (options.method === 'DELETE') return Response.json({ id: 'task-1', status: 'cancelled' })
    return Response.json({ id: 'task-1', status: 'completed', output: 'fake result' })
  }
  const handle = createHttpHandler('http://relay.test', fakeFetch)
  const listed = await handle({ method: 'tools/list' })
  assert.deepEqual(listed.tools.map(tool => tool.name), ['list_agents', 'delegate', 'get_task', 'cancel_task'])
  const agents = await handle({ method: 'tools/call', params: { name: 'list_agents', arguments: {} } })
  assert.equal(agents.structuredContent.agents[0].id, 'fake')
  const submitted = await handle({ method: 'tools/call', params: { name: 'delegate', arguments: { agentId: 'fake', input: 'do work', sessionId: 'session-1', requestId: 'request-1' } } })
  assert.equal(submitted.structuredContent.id, 'task-1')
  assert.deepEqual(JSON.parse(calls[1].options.body), { agentId: 'fake', input: 'do work', sessionId: 'session-1', requestId: 'request-1' })
  const completed = await handle({ method: 'tools/call', params: { name: 'get_task', arguments: { taskId: 'task-1' } } })
  assert.equal(completed.structuredContent.output, 'fake result')
  const cancelled = await handle({ method: 'tools/call', params: { name: 'cancel_task', arguments: { taskId: 'task-1' } } })
  assert.equal(cancelled.structuredContent.status, 'cancelled')
  assert.equal(calls[3].options.method, 'DELETE')
})

test('returns relay failures as MCP tool errors', async () => {
  const handle = createHttpHandler('http://relay.test', async () => Response.json({ error: 'unknown_task' }, { status: 404 }))
  const result = await handle({ method: 'tools/call', params: { name: 'get_task', arguments: { taskId: 'missing' } } })
  assert.equal(result.isError, true)
  assert.deepEqual(result.structuredContent, { error: 'unknown_task', message: 'unknown_task', status: 404 })

  const malformed = createHttpHandler('http://relay.test', async () => Response.json('bad gateway', { status: 502 }))
  const malformedResult = await malformed({ method: 'tools/call', params: { name: 'list_agents', arguments: {} } })
  assert.equal(malformedResult.isError, true)
  assert.deepEqual(malformedResult.structuredContent, { error: 'invalid_relay_response', message: 'invalid_relay_response', status: 502 })
})

test('validates tool arguments before calling the relay', async () => {
  let calls = 0
  const handle = createHttpHandler('http://relay.test', async () => { calls++; return Response.json({}) })
  await assert.rejects(
    handle({ method: 'tools/call', params: { name: 'delegate', arguments: { agentId: 'fake', input: '', extra: true } } }),
    error => error.code === -32602
  )
  await assert.rejects(
    handle({ method: 'tools/call', params: { name: 'delegate', arguments: { agentId: 'fake', input: 'work', timeoutMs: 1.5 } } }),
    error => error.code === -32602
  )
  assert.equal(calls, 0)
})

test('stdio reports malformed requests and does not serialize independent calls', async () => {
  const input = new PassThrough()
  const output = new PassThrough()
  let finishSlow
  const slow = new Promise(resolve => { finishSlow = resolve })
  const handle = async rpc => {
    if (rpc.method === 'slow') { await slow; return { done: true } }
    return { pong: true }
  }
  let data = ''
  output.on('data', chunk => { data += chunk })
  const running = serve(input, output, handle)
  input.write('null\n')
  input.write('{bad json\n')
  input.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'slow' })}\n`)
  input.write(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'ping' })}\n`)
  await new Promise(resolve => setImmediate(resolve))
  const early = data.trim().split('\n').map(line => JSON.parse(line))
  assert.equal(early[0].error.code, -32600)
  assert.equal(early[1].error.code, -32700)
  assert.equal(early[2].id, 2)
  finishSlow()
  input.end()
  await running
  const responses = data.trim().split('\n').map(line => JSON.parse(line))
  assert.equal(responses[3].id, 1)
})

test('executable entry point serves stdio when its path contains spaces', async () => {
  const child = spawn(process.execPath, ['src/mcp-server.mjs'], {
    cwd: new URL('..', import.meta.url), stdio: ['pipe', 'pipe', 'pipe']
  })
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]()
  const timeout = setTimeout(() => child.kill('SIGTERM'), 2000)
  try {
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'fake', version: '1' } } })}\n`)
    const next = await lines.next()
    assert.equal(next.done, false)
    const response = JSON.parse(next.value)
    assert.equal(response.id, 7)
    assert.equal(response.result.protocolVersion, '2025-06-18')
  } finally {
    clearTimeout(timeout)
    child.stdin.end()
    child.kill('SIGTERM')
  }
})
