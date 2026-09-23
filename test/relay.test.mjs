import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn } from 'node:child_process'
import http from 'node:http'

async function relay(t, agent, env = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'a2a-http-')); const config = join(dir, 'agents.json')
  await writeFile(config, JSON.stringify({ agents: [agent] }))
  const port = 44000 + Math.floor(Math.random() * 1000)
  const child = spawn(process.execPath, ['src/server.mjs'], { cwd: new URL('..', import.meta.url), env: { ...process.env, A2A_RELAY_PORT: port, A2A_AGENTS_FILE: config, ...env } })
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('relay did not start')), 3000)
    let stderr = ''
    child.stderr.on('data', data => { stderr += data })
    child.stdout.on('data', data => { if (data.toString().includes('listening')) { clearTimeout(timer); resolve() } })
    child.once('error', reject)
    child.once('exit', code => { clearTimeout(timer); reject(new Error(`relay exited with ${code}: ${stderr.trim()}`)) })
  })
  t.after(async () => { child.kill(); await rm(dir, { recursive: true, force: true }) })
  t.relayChild = child
  return `http://127.0.0.1:${port}`
}

async function waitFor(base, id) {
  for (let i = 0; i < 80; i++) {
    const task = await (await fetch(`${base}/v1/tasks/${id}`)).json()
    if (!['queued', 'running'].includes(task.status)) return task
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  throw new Error('task did not finish')
}

test('submits and polls a command task with explicit session identity', async t => {
  const base = await relay(t, { id: 'echo', command: process.execPath, args: ['-e', 'console.log(process.env.A2A_SESSION_ID + ":" + process.argv[1])'] })
  const response = await fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'echo', sessionId: 'session-7', input: 'hello' }) })
  assert.equal(response.status, 202); const submitted = await response.json()
  assert.match(submitted.id, /^[0-9a-f-]{36}$/); assert.equal(submitted.sessionId, 'session-7')
  const task = await waitFor(base, submitted.id)
  assert.equal(task.status, 'completed'); assert.equal(task.output, 'session-7:hello')
})

test('times out command tasks', async t => {
  const base = await relay(t, { id: 'slow', command: process.execPath, args: ['-e', 'setTimeout(() => {}, 10000)'] }, { A2A_RELAY_TIMEOUT_MS: '50' })
  const submitted = await (await fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'slow', input: 'wait' }) })).json()
  assert.equal((await waitFor(base, submitted.id)).status, 'timed_out')
})

test('cancels tasks and bounds captured output', async t => {
  const slow = await relay(t, { id: 'output', command: process.execPath, args: ['-e', 'process.stdout.write("x".repeat(5000)); setTimeout(() => {}, 10000)'] }, { A2A_RELAY_MAX_OUTPUT_BYTES: '32' })
  const first = await (await fetch(`${slow}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'output', input: 'x' }) })).json()
  await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal((await (await fetch(`${slow}/v1/tasks/${first.id}`, { method: 'DELETE' })).json()).status, 'cancelled')

  const quick = await relay(t, { id: 'output', command: process.execPath, args: ['-e', 'process.stdout.write("x".repeat(5000))'] }, { A2A_RELAY_MAX_OUTPUT_BYTES: '32' })
  const second = await (await fetch(`${quick}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'output', input: 'x' }) })).json()
  const bounded = await waitFor(quick, second.id)
  assert.equal(Buffer.byteLength(bounded.output), 32); assert.equal(bounded.outputTruncated, true)
})

test('queues at the concurrency limit and evicts the oldest terminal task', async t => {
  const base = await relay(t, { id: 'work', command: process.execPath, args: ['-e', 'setTimeout(() => console.log(process.argv[1]), 100)'] }, { A2A_RELAY_MAX_ACTIVE: '1', A2A_RELAY_MAX_TASKS: '2' })
  const submit = input => fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'work', input }) }).then(response => response.json())
  const first = await submit('one'); const second = await submit('two')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal((await (await fetch(`${base}/v1/tasks/${second.id}`)).json()).status, 'queued')
  await waitFor(base, first.id)
  const third = await submit('three')
  assert.ok(third.id)
  assert.equal((await fetch(`${base}/v1/tasks/${first.id}`)).status, 404)
})

test('rejects invalid task timeout and does not inherit unrelated environment', async t => {
  const base = await relay(t, { id: 'env', command: process.execPath, args: ['-e', 'console.log(process.env.RELAY_TEST_SECRET || "clean")'] }, { RELAY_TEST_SECRET: 'must-not-leak' })
  const invalid = await fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'env', input: 'x', timeoutMs: -1 }) })
  assert.equal(invalid.status, 400)
  const submitted = await (await fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'env', input: 'x' }) })).json()
  assert.equal((await waitFor(base, submitted.id)).output, 'clean')

  const optedIn = await relay(t, { id: 'env', command: process.execPath, args: ['-e', 'console.log(process.env.RELAY_TEST_SECRET)'], inheritEnv: ['RELAY_TEST_SECRET'] }, { RELAY_TEST_SECRET: 'available' })
  const allowed = await (await fetch(`${optedIn}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'env', input: 'x' }) })).json()
  assert.equal((await waitFor(optedIn, allowed.id)).output, 'available')
})

test('reports invalid registry configuration without a stack trace', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'a2a-invalid-')); const config = join(dir, 'agents.json')
  await writeFile(config, JSON.stringify({ agents: [{ id: 'bad', command: process.execPath, args: 'wrong', env: { TOKEN: 3 } }] }))
  const child = spawn(process.execPath, ['src/server.mjs'], { cwd: new URL('..', import.meta.url), env: { ...process.env, A2A_AGENTS_FILE: config } })
  let stderr = ''; child.stderr.on('data', data => { stderr += data })
  const code = await new Promise(resolve => child.once('exit', resolve))
  await rm(dir, { recursive: true, force: true }); t.after(() => child.kill())
  assert.equal(code, 1)
  assert.match(stderr, /^Configuration error: Invalid command agent: bad/)
  assert.doesNotMatch(stderr, /\n\s+at /)
})

test('returns 413 for oversized requests and command arguments', async t => {
  const bodyLimited = await relay(t, { id: 'echo', command: process.execPath, args: ['-e', 'console.log(process.argv[1])'] }, { A2A_RELAY_MAX_BODY_BYTES: '64' })
  const oversizedBody = await fetch(`${bodyLimited}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'echo', input: 'x'.repeat(100) }) })
  assert.equal(oversizedBody.status, 413)

  const argumentLimited = await relay(t, { id: 'echo', command: process.execPath, args: ['-e', 'console.log(process.argv[1])'] }, { A2A_RELAY_MAX_COMMAND_INPUT_BYTES: '8' })
  const oversizedArgument = await fetch(`${argumentLimited}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'echo', input: 'x'.repeat(9) }) })
  assert.equal(oversizedArgument.status, 413)
  assert.equal((await oversizedArgument.json()).error, 'command_input_too_large')
})

test('deduplicates task submission by request ID and rejects conflicting reuse', async t => {
  const base = await relay(t, { id: 'echo', command: process.execPath, args: ['-e', 'console.log(process.argv[1])'] })
  const submit = input => fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'echo', requestId: 'caller-1', input }) })
  const firstResponse = await submit('one'); const first = await firstResponse.json()
  const replayResponse = await submit('one'); const replay = await replayResponse.json()
  assert.equal(firstResponse.status, 202); assert.equal(replayResponse.status, 200); assert.equal(replay.id, first.id)
  const conflict = await submit('two')
  assert.equal(conflict.status, 409); assert.equal((await conflict.json()).error, 'idempotency_conflict')
})

test('terminates running agent processes when the relay shuts down', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'a2a-shutdown-'))
  const marker = join(dir, 'killed'); const ready = join(dir, 'ready')
  const script = "process.on('SIGTERM', () => { require('node:fs').writeFileSync(process.env.MARKER, 'killed'); process.exit(0) }); require('node:fs').writeFileSync(process.env.READY, 'ready'); setTimeout(() => {}, 60000)"
  const base = await relay(t, { id: 'sleeper', command: process.execPath, args: ['-e', script], env: { MARKER: marker, READY: ready } })
  const submitted = await (await fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'sleeper', input: 'wait' }) })).json()
  for (let i = 0; i < 120; i++) {
    try { await stat(ready); break } catch {}
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  assert.equal((await (await fetch(`${base}/v1/tasks/${submitted.id}`)).json()).status, 'running')
  t.relayChild.kill('SIGTERM')
  let killed = false
  for (let i = 0; i < 120; i++) {
    try { await stat(marker); killed = true; break } catch {}
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  await rm(dir, { recursive: true, force: true })
  assert.equal(killed, true)
})

test('returns 405 for known routes with the wrong method', async t => {
  const base = await relay(t, { id: 'echo', command: process.execPath, args: ['-e', 'console.log(process.argv[1])'] })
  for (const [method, path] of [['PUT', '/v1/tasks'], ['POST', '/v1/agents'], ['DELETE', '/healthz'], ['POST', '/v1/tasks/unknown-id']]) {
    const response = await fetch(`${base}${path}`, { method })
    assert.equal(response.status, 405, `${method} ${path}`)
    assert.equal((await response.json()).error, 'method_not_allowed')
  }
})

test('bounds HTTP adapter responses while streaming', async t => {
  const upstream = http.createServer((req, res) => { res.write('x'.repeat(24)); res.end('y'.repeat(24)) })
  const upstreamPort = await new Promise(resolve => upstream.listen(0, '127.0.0.1', () => resolve(upstream.address().port)))
  t.after(() => upstream.close())
  const base = await relay(t, { id: 'remote', type: 'http', url: `http://127.0.0.1:${upstreamPort}` }, { A2A_RELAY_MAX_OUTPUT_BYTES: '32' })
  const submitted = await (await fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentId: 'remote', input: 'work' }) })).json()
  const completed = await waitFor(base, submitted.id)
  assert.equal(completed.status, 'completed'); assert.equal(Buffer.byteLength(completed.output), 32); assert.equal(completed.outputTruncated, true)
})
