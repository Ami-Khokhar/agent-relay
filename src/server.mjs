import http from 'node:http'
import { randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'

const host = process.env.A2A_RELAY_HOST ?? '127.0.0.1'
const configPath = process.env.A2A_AGENTS_FILE ?? new URL('../config/agents.json', import.meta.url)
function positiveInteger(name, fallback) {
  const value = Number(process.env[name] ?? fallback)
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${name} must be a positive integer`)
  return value
}
const port = positiveInteger('A2A_RELAY_PORT', 43124)
const maxBody = positiveInteger('A2A_RELAY_MAX_BODY_BYTES', 1_048_576)
const timeout = positiveInteger('A2A_RELAY_TIMEOUT_MS', 120_000)
const maxOutput = positiveInteger('A2A_RELAY_MAX_OUTPUT_BYTES', 262_144)
const maxTasks = positiveInteger('A2A_RELAY_MAX_TASKS', 1000)
const maxActive = positiveInteger('A2A_RELAY_MAX_ACTIVE', 4)
const maxCommandInput = positiveInteger('A2A_RELAY_MAX_COMMAND_INPUT_BYTES', 65_536)

function json(res, status, value) {
  const body = JSON.stringify(value)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) }).end(body)
}

function body(req) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0; let tooLarge = false
    req.on('data', chunk => {
      size += chunk.length
      if (size > maxBody) {
        if (!tooLarge) reject(Object.assign(new Error('body too large'), { status: 413 }))
        tooLarge = true
      } else chunks.push(chunk)
    })
    req.on('end', () => { if (!tooLarge) resolve(Buffer.concat(chunks)) })
    req.on('error', reject)
  })
}

async function registry() {
  const config = JSON.parse(await readFile(configPath, 'utf8'))
  if (!Array.isArray(config.agents)) throw new Error('config.agents must be an array')
  const result = new Map()
  for (const raw of config.agents) {
    const type = raw.type ?? (raw.command ? 'command' : 'http')
    if (!raw.id || !/^[\w.~-]+$/.test(raw.id) || result.has(raw.id)) throw new Error(`Invalid or duplicate agent: ${raw.id}`)
    if (['command', 'stdio'].includes(type) && (typeof raw.command !== 'string' || !Array.isArray(raw.args ?? []) || !(raw.args ?? []).every(value => typeof value === 'string'))) throw new Error(`Invalid ${type} agent: ${raw.id}`)
    if (raw.inheritEnv !== undefined && (!Array.isArray(raw.inheritEnv) || !raw.inheritEnv.every(value => typeof value === 'string' && /^[A-Za-z_][A-Za-z0-9_]*$/.test(value)))) throw new Error(`Invalid inherited environment: ${raw.id}`)
    if (raw.env !== undefined && (raw.env === null || Array.isArray(raw.env) || typeof raw.env !== 'object' || !Object.values(raw.env).every(value => typeof value === 'string'))) throw new Error(`Invalid agent env: ${raw.id}`)
    if (raw.timeoutMs !== undefined && (!Number.isSafeInteger(raw.timeoutMs) || raw.timeoutMs <= 0)) throw new Error(`Invalid agent timeout: ${raw.id}`)
    if (type === 'http' && !['http:', 'https:'].includes(new URL(raw.url).protocol)) throw new Error(`Invalid HTTP agent: ${raw.id}`)
    if (raw.capabilities !== undefined && (raw.capabilities === null || Array.isArray(raw.capabilities) || typeof raw.capabilities !== 'object' || !Object.values(raw.capabilities).every(value => typeof value === 'boolean'))) throw new Error(`Invalid agent capabilities: ${raw.id}`)
    if (raw.capabilities?.newTasks === false || raw.capabilities?.nativeSessions === true || raw.capabilities?.streaming === true) throw new Error(`Unsupported agent capabilities: ${raw.id}`)
    if (!['command', 'stdio', 'http'].includes(type)) throw new Error(`Unknown adapter: ${type}`)
    result.set(raw.id, { ...raw, type, args: raw.args ?? [], capabilities: { newTasks: true, nativeSessions: false, streaming: false, ...(raw.capabilities ?? {}) } })
  }
  return result
}

function command(agent, task, done) {
  const child = spawn(agent.command, [...agent.args, task.input], {
    cwd: agent.cwd,
    env: { ...environment(agent), A2A_TASK_ID: task.id, A2A_SESSION_ID: task.sessionId },
    stdio: ['ignore', 'pipe', 'pipe']
  })
  let output = ''; let error = ''; let captured = 0; let truncated = false
  const capture = (target, chunk) => {
    const bytes = Buffer.from(chunk); const remaining = maxOutput - captured
    if (remaining <= 0) { truncated = true; return target }
    const kept = bytes.subarray(0, remaining); captured += kept.length
    truncated ||= kept.length < bytes.length
    return target + kept.toString()
  }
  child.stdout.on('data', chunk => { output = capture(output, chunk) })
  child.stderr.on('data', chunk => { error = capture(error, chunk) })
  child.once('error', reason => done('failed', { error: reason.message }))
  child.once('close', code => {
    if (task.status !== 'running') return
    done(code === 0 ? 'completed' : 'failed', { output: output.trim(), error: code ? error.trim() || `Agent exited with ${code}` : undefined, outputTruncated: truncated })
  })
  return () => { child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 1000).unref() }
}

function stdioAgent(agent, task, done) {
  const child = spawn(agent.command, agent.args, { cwd: agent.cwd, env: environment(agent), stdio: ['pipe', 'pipe', 'pipe'] })
  const output = []; const error = []; let captured = 0; let truncated = false
  const capture = (target, chunk) => {
    const bytes = Buffer.from(chunk); const remaining = maxOutput - captured
    if (remaining <= 0) { truncated = true; return }
    const kept = bytes.subarray(0, remaining); captured += kept.length
    truncated ||= kept.length < bytes.length
    target.push(kept)
  }
  child.stdout.on('data', chunk => capture(output, chunk))
  child.stderr.on('data', chunk => capture(error, chunk))
  child.stdin.on('error', reason => done('failed', { error: `Adapter stdin failed: ${reason.message}` }))
  child.once('error', reason => done('failed', { error: reason.message }))
  child.once('close', code => {
    if (task.status !== 'running') return
    const stdout = Buffer.concat(output).toString('utf8'); const stderr = Buffer.concat(error).toString('utf8')
    if (code !== 0) return done('failed', { error: stderr.trim() || `Adapter exited with ${code}`, outputTruncated: truncated })
    if (truncated) return done('failed', { error: 'Adapter response exceeded output limit', outputTruncated: true })
    let response
    try { response = JSON.parse(stdout) } catch { return done('failed', { error: 'Adapter returned invalid JSON' }) }
    if (!response || typeof response !== 'object' || Array.isArray(response) || response.protocolVersion !== 'relay.adapter/v1' || !['completed', 'failed'].includes(response.status)) return done('failed', { error: 'Adapter returned an invalid relay.adapter/v1 response' })
    if (response.output !== undefined && typeof response.output !== 'string') return done('failed', { error: 'Adapter response output must be a string' })
    if (response.error !== undefined && typeof response.error !== 'string') return done('failed', { error: 'Adapter response error must be a string' })
    if (response.status === 'failed' && !response.error) return done('failed', { error: 'Adapter reported failure without an error' })
    done(response.status, { output: response.output ?? '', error: response.error })
  })
  child.stdin.end(JSON.stringify(adapterRequest(task)) + '\n')
  return () => { child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 1000).unref() }
}

function environment(agent) {
  const inherited = {}
  for (const key of ['PATH', 'HOME', 'USER', 'SHELL', 'TMPDIR', 'LANG', 'LC_ALL', 'SystemRoot', 'ComSpec', 'PATHEXT']) {
    if (process.env[key] !== undefined) inherited[key] = process.env[key]
  }
  for (const key of agent.inheritEnv ?? []) if (process.env[key] !== undefined) inherited[key] = process.env[key]
  return { ...inherited, ...(agent.env ?? {}), A2A_ADAPTER_PROTOCOL: 'relay.adapter/v1' }
}

function adapterRequest(task) {
  return { protocolVersion: 'relay.adapter/v1', task: { id: task.id, sessionId: task.sessionId, input: task.input, timeoutMs: task.timeoutMs } }
}

function httpAgent(agent, task, done) {
  const controller = new AbortController()
  fetch(agent.url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(adapterRequest(task)), signal: controller.signal })
    .then(async response => {
      const chunks = []; let size = 0; let truncated = false
      if (response.body) for await (const chunk of response.body) {
        const bytes = Buffer.from(chunk); const remaining = maxOutput - size
        if (remaining > 0) { chunks.push(bytes.subarray(0, remaining)); size += Math.min(bytes.length, remaining) }
        if (bytes.length > remaining) { truncated = true; break }
      }
      const raw = Buffer.concat(chunks, size).toString()
      const isJson = response.headers.get('content-type')?.toLowerCase().includes('application/json')
      if (truncated) return isJson
        ? done('failed', { error: 'Adapter response exceeded output limit', outputTruncated: true })
        : done(response.ok ? 'completed' : 'failed', { output: raw, error: response.ok ? undefined : `HTTP agent returned ${response.status}`, outputTruncated: true })
      if (!isJson) return done(response.ok ? 'completed' : 'failed', { output: raw, error: response.ok ? undefined : `HTTP agent returned ${response.status}` })
      let value
      try { value = JSON.parse(raw) } catch { return done('failed', { error: 'HTTP adapter returned invalid JSON' }) }
      if (!value || typeof value !== 'object' || Array.isArray(value) || value.protocolVersion !== 'relay.adapter/v1' || !['completed', 'failed'].includes(value.status)) return done('failed', { error: 'HTTP adapter returned an invalid relay.adapter/v1 response' })
      if (value.output !== undefined && typeof value.output !== 'string') return done('failed', { error: 'Adapter response output must be a string' })
      if (value.error !== undefined && typeof value.error !== 'string') return done('failed', { error: 'Adapter response error must be a string' })
      if (value.status === 'failed' && !value.error) return done('failed', { error: 'Adapter reported failure without an error' })
      if (!response.ok) return done('failed', { error: value.error || `HTTP agent returned ${response.status}`, output: value.output ?? '' })
      done(value.status, { output: value.output ?? '', error: value.error })
    }).catch(reason => { if (task.status === 'running') done('failed', { error: reason.message }) })
  return () => controller.abort()
}

const agents = await registry().catch(error => {
  console.error(`Configuration error: ${error.message}`)
  process.exit(1)
})
const tasks = new Map()
const requestIds = new Map()
const queue = []
let active = 0
const visible = task => Object.fromEntries(Object.entries(task).filter(([key]) => !['cancel', 'timer', 'requestKey', 'requestedSessionId', 'requestedTimeoutMs'].includes(key)))

function start(agent, task) {
  active++
  task.status = 'running'; task.startedAt = new Date().toISOString()
  const done = (status, fields = {}) => {
    if (task.status !== 'running') return
    clearTimeout(task.timer); task.status = status; task.finishedAt = new Date().toISOString(); Object.assign(task, fields)
    delete task.cancel; delete task.timer; active--; drain()
  }
  try { task.cancel = (agent.type === 'command' ? command : agent.type === 'stdio' ? stdioAgent : httpAgent)(agent, task, done) }
  catch (error) { done('failed', { error: error.message }); return }
  task.timer = setTimeout(() => { task.cancel?.(); done('timed_out', { error: `Agent exceeded ${task.timeoutMs} ms` }) }, task.timeoutMs)
}

function drain() {
  while (active < maxActive && queue.length) {
    const task = queue.shift()
    if (task.status === 'queued') start(agents.get(task.agentId), task)
  }
}

function makeRoom() {
  if (tasks.size < maxTasks) return true
  const terminal = [...tasks.values()].find(task => !['queued', 'running'].includes(task.status))
  if (!terminal) return false
  tasks.delete(terminal.id)
  if (terminal.requestKey) requestIds.delete(terminal.requestKey)
  return true
}

const valid = (value, limit = 128) => typeof value === 'string' && value.length > 0 && value.length <= limit
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${host}:${port}`)
    if (req.method === 'GET' && url.pathname === '/healthz') return json(res, 200, { ok: true, agents: agents.size, tasks: tasks.size })
    if (req.method === 'GET' && url.pathname === '/v1/agents') return json(res, 200, { agents: [...agents.values()].map(({ id, name, description, type, capabilities }) => ({ id, name: name ?? id, description, adapter: type, capabilities: { ...capabilities, cancellation: type === 'http' ? 'request_only' : 'process_signal' } })) })
    if (req.method === 'POST' && url.pathname === '/v1/tasks') {
      const raw = await body(req)
      let request
      try { request = JSON.parse(raw.toString() || '{}') } catch { return json(res, 400, { error: 'invalid_json' }) }
      if (!request || typeof request !== 'object' || Array.isArray(request)) return json(res, 400, { error: 'invalid_request' })
      const agent = agents.get(request.agentId)
      if (!agent) return json(res, 404, { error: 'unknown_agent' })
      if (!valid(request.input, maxBody)) return json(res, 400, { error: 'input_required' })
      if (request.sessionId !== undefined && !valid(request.sessionId)) return json(res, 400, { error: 'invalid_session_id' })
      if (request.requestId !== undefined && !valid(request.requestId)) return json(res, 400, { error: 'invalid_request_id' })
      if (request.timeoutMs !== undefined && (!Number.isSafeInteger(request.timeoutMs) || request.timeoutMs <= 0)) return json(res, 400, { error: 'invalid_timeout' })
      if (agent.type === 'command' && Buffer.byteLength(request.input) > maxCommandInput) return json(res, 413, { error: 'command_input_too_large' })
      const requestKey = request.requestId === undefined ? undefined : `${agent.id}\0${request.requestId}`
      const prior = requestKey && requestIds.get(requestKey)
      if (prior) {
        if (prior.input !== request.input || prior.requestedSessionId !== request.sessionId || prior.requestedTimeoutMs !== request.timeoutMs) return json(res, 409, { error: 'idempotency_conflict' })
        return json(res, 200, visible(prior))
      }
      if (!makeRoom()) return json(res, 503, { error: 'task_capacity_reached' })
      const taskTimeout = Math.min(request.timeoutMs ?? agent.timeoutMs ?? timeout, timeout)
      const task = { id: randomUUID(), sessionId: request.sessionId ?? randomUUID(), agentId: agent.id, input: request.input, status: 'queued', createdAt: new Date().toISOString(), timeoutMs: taskTimeout }
      if (requestKey) {
        task.requestId = request.requestId; task.requestKey = requestKey
        task.requestedSessionId = request.sessionId; task.requestedTimeoutMs = request.timeoutMs
        requestIds.set(requestKey, task)
      }
      tasks.set(task.id, task); queue.push(task); queueMicrotask(drain)
      return json(res, 202, visible(task))
    }
    const match = url.pathname.match(/^\/v1\/tasks\/([^/]+)$/); const task = match && tasks.get(match[1])
    if (match && req.method === 'GET') return task ? json(res, 200, visible(task)) : json(res, 404, { error: 'unknown_task' })
    if (match && req.method === 'DELETE') {
      if (!task) return json(res, 404, { error: 'unknown_task' })
      if (['queued', 'running'].includes(task.status)) {
        if (task.status === 'running') { clearTimeout(task.timer); task.cancel?.(); active--; queueMicrotask(drain) }
        else {
          const index = queue.indexOf(task)
          if (index >= 0) queue.splice(index, 1)
        }
        task.status = 'cancelled'; task.finishedAt = new Date().toISOString(); delete task.cancel; delete task.timer
      }
      return json(res, 200, visible(task))
    }
    if (match || url.pathname === '/v1/tasks' || url.pathname === '/v1/agents' || url.pathname === '/healthz') return json(res, 405, { error: 'method_not_allowed' })
    json(res, 404, { error: 'not_found' })
  } catch (error) { json(res, error.status ?? 500, { error: 'request_failed', message: error.message }) }
})

let shuttingDown = false
function shutdown(signal) {
  if (shuttingDown) return
  shuttingDown = true
  const running = [...tasks.values()].filter(task => task.status === 'running')
  if (running.length) console.log(`Received ${signal}; cancelling ${running.length} running task(s)`)
  for (const task of running) task.cancel?.()
  const exit = () => process.exit(0)
  server.close()
  // Adapter cancellation escalates to SIGKILL after 1s; wait it out before exiting.
  setTimeout(exit, running.length ? 1500 : 0).unref()
}
process.once('SIGTERM', () => shutdown('SIGTERM'))
process.once('SIGINT', () => shutdown('SIGINT'))

server.once('error', error => {
  console.error(`Relay failed to listen: ${error.message}`)
  process.exitCode = 1
})
server.listen(port, host, () => console.log(`A2A relay listening at http://${host}:${port}`))
