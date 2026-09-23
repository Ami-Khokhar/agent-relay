import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn } from 'node:child_process'
import http from 'node:http'
import { fileURLToPath } from 'node:url'

async function relay(t, agent, env = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'relay-adapter-')); const config = join(dir, 'agents.json')
  await writeFile(config, JSON.stringify({ agents: [agent] }))
  const port = 45000 + Math.floor(Math.random() * 1000)
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
  return `http://127.0.0.1:${port}`
}

async function run(base, body) {
  const submitted = await (await fetch(`${base}/v1/tasks`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })).json()
  for (let i = 0; i < 100; i++) {
    const task = await (await fetch(`${base}/v1/tasks/${submitted.id}`)).json()
    if (!['queued', 'running'].includes(task.status)) return task
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error('task did not finish')
}

const fake = fileURLToPath(new URL('./fixtures/fake-stdio-adapter.mjs', import.meta.url))

test('stdio adapter works from config with multiline input and reports capabilities', async t => {
  const base = await relay(t, { id: 'any-harness', type: 'stdio', command: process.execPath, args: [fake] })
  const listed = await (await fetch(`${base}/v1/agents`)).json()
  assert.deepEqual(listed.agents[0].capabilities, { newTasks: true, nativeSessions: false, streaming: false, cancellation: 'process_signal' })
  const input = `first line\n${'é'.repeat(5000)}\nlast line`
  const task = await run(base, { agentId: 'any-harness', sessionId: 'correlation-1', input })
  assert.equal(task.status, 'completed')
  assert.equal(task.output, `correlation-1:${input}`)
})

test('stdio adapter normalizes reported, malformed, and oversized failures', async t => {
  for (const [mode, expected] of [['failed', 'fake failure'], ['malformed', 'invalid JSON'], ['oversized', 'exceeded output limit'], ['early-exit', 'invalid JSON|stdin failed']]) {
    await t.test(mode, async t => {
      const base = await relay(t, { id: 'fake', type: 'stdio', command: process.execPath, args: [fake], env: { FAKE_ADAPTER_MODE: mode } }, { A2A_RELAY_MAX_OUTPUT_BYTES: mode === 'oversized' ? '128' : '262144' })
      const task = await run(base, { agentId: 'fake', input: 'work' })
      assert.equal(task.status, 'failed'); assert.match(task.error, new RegExp(expected))
    })
  }
})

test('hosted HTTP adapters use the same request and result envelopes', async t => {
  let received
  const upstream = http.createServer(async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk
    received = JSON.parse(raw)
    res.setHeader('content-type', 'application/json')
    res.end(JSON.stringify({ protocolVersion: 'relay.adapter/v1', status: 'completed', output: `hosted:${received.task.input}` }))
  })
  const port = await new Promise(resolve => upstream.listen(0, '127.0.0.1', () => resolve(upstream.address().port)))
  t.after(() => upstream.close())
  const base = await relay(t, { id: 'hosted', type: 'http', url: `http://127.0.0.1:${port}` })
  const task = await run(base, { agentId: 'hosted', input: 'work' })
  assert.equal(received.protocolVersion, 'relay.adapter/v1')
  assert.equal(received.task.id, task.id)
  assert.equal(task.status, 'completed'); assert.equal(task.output, 'hosted:work')
  const listed = await (await fetch(`${base}/v1/agents`)).json()
  assert.equal(listed.agents[0].capabilities.cancellation, 'request_only')
})
