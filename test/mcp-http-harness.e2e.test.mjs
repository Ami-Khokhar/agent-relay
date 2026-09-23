import test from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'

const projectRoot = new URL('..', import.meta.url)

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  return server.address().port
}

async function unusedPort() {
  const server = http.createServer()
  const port = await listen(server)
  await new Promise(resolve => server.close(resolve))
  return port
}

async function startRelay(t, config) {
  const directory = await mkdtemp(join(tmpdir(), 'relay-mcp-e2e-'))
  const configPath = join(directory, 'agents.json')
  await writeFile(configPath, JSON.stringify(config))
  const port = await unusedPort()
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: projectRoot,
    env: { ...process.env, A2A_RELAY_PORT: String(port), A2A_AGENTS_FILE: configPath },
    stdio: ['ignore', 'pipe', 'pipe']
  })
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('relay did not start')), 3000)
    let stderr = ''
    child.stderr.on('data', chunk => { stderr += chunk })
    child.stdout.on('data', chunk => {
      if (chunk.toString().includes('listening')) {
        clearTimeout(timer)
        resolve()
      }
    })
    child.once('error', reject)
    child.once('exit', code => {
      clearTimeout(timer)
      reject(new Error(`relay exited with ${code}: ${stderr.trim()}`))
    })
  })
  t.after(async () => {
    child.kill('SIGTERM')
    await rm(directory, { recursive: true, force: true })
  })
  return `http://127.0.0.1:${port}`
}

function startMcp(t, relayUrl) {
  const child = spawn(process.execPath, ['src/mcp-server.mjs'], {
    cwd: projectRoot,
    env: { ...process.env, A2A_RELAY_URL: relayUrl },
    stdio: ['pipe', 'pipe', 'pipe']
  })
  const pending = new Map()
  let nextId = 1
  let stderr = ''
  child.stderr.on('data', chunk => { stderr += chunk })
  createInterface({ input: child.stdout }).on('line', line => {
    const response = JSON.parse(line)
    const request = pending.get(response.id)
    if (!request) return
    pending.delete(response.id)
    if (response.error) request.reject(new Error(response.error.message))
    else request.resolve(response.result)
  })
  child.once('exit', code => {
    for (const { reject } of pending.values()) reject(new Error(`MCP server exited with ${code}: ${stderr.trim()}`))
    pending.clear()
  })
  t.after(() => child.kill('SIGTERM'))
  return (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId++
    pending.set(id, { resolve, reject })
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`)
  })
}

test('delegates through MCP and the relay to a harness on its own HTTP port', async t => {
  let received
  const harness = http.createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    received = JSON.parse(Buffer.concat(chunks).toString())
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({
      protocolVersion: 'relay.adapter/v1',
      status: 'completed',
      output: `fake harness completed: ${received.task.input}`
    }))
  })
  const harnessPort = await listen(harness)
  t.after(() => harness.close())

  const relayUrl = await startRelay(t, { agents: [{
    id: 'portable-harness',
    name: 'Portable fake harness',
    type: 'http',
    url: `http://127.0.0.1:${harnessPort}/tasks`
  }] })
  const call = startMcp(t, relayUrl)

  const initialized = await call('initialize', {
    protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'e2e-test', version: '1' }
  })
  assert.equal(initialized.protocolVersion, '2025-06-18')

  const agentsResult = await call('tools/call', { name: 'list_agents', arguments: {} })
  assert.equal(agentsResult.structuredContent.agents[0].id, 'portable-harness')
  assert.equal(agentsResult.structuredContent.agents[0].adapter, 'http')

  const delegated = await call('tools/call', {
    name: 'delegate',
    arguments: { agentId: 'portable-harness', input: 'review this change', requestId: 'e2e-request-1' }
  })
  const taskId = delegated.structuredContent.id
  assert.ok(taskId)

  let task
  for (let attempt = 0; attempt < 80; attempt++) {
    const result = await call('tools/call', { name: 'get_task', arguments: { taskId } })
    task = result.structuredContent
    if (!['queued', 'running'].includes(task.status)) break
    await new Promise(resolve => setTimeout(resolve, 25))
  }

  assert.equal(task.status, 'completed')
  assert.equal(task.output, 'fake harness completed: review this change')
  assert.equal(received.protocolVersion, 'relay.adapter/v1')
  assert.equal(received.task.input, 'review this change')
  assert.equal(received.task.id, taskId)
  assert.equal(received.task.sessionId, task.sessionId)
  assert.equal(received.task.timeoutMs, task.timeoutMs)
})
