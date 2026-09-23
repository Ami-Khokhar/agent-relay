#!/usr/bin/env node

import { createInterface } from 'node:readline'
import { pathToFileURL } from 'node:url'

const relayUrl = (process.env.A2A_RELAY_URL ?? 'http://127.0.0.1:43124').replace(/\/$/, '')
const requestTimeoutMs = Number(process.env.A2A_RELAY_HTTP_TIMEOUT_MS ?? 10_000)
if (!Number.isSafeInteger(requestTimeoutMs) || requestTimeoutMs <= 0) throw new Error('A2A_RELAY_HTTP_TIMEOUT_MS must be a positive integer')

const tools = [
  {
    name: 'list_agents', description: 'List agents registered with the relay',
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
    annotations: { readOnlyHint: true, openWorldHint: true }
  },
  {
    name: 'delegate', description: 'Submit an asynchronous task to a registered agent',
    inputSchema: {
      type: 'object', required: ['agentId', 'input'], additionalProperties: false,
      properties: {
        agentId: { type: 'string', minLength: 1, maxLength: 128 }, input: { type: 'string', minLength: 1 },
        sessionId: { type: 'string', minLength: 1, maxLength: 128 }, requestId: { type: 'string', minLength: 1, maxLength: 128 },
        timeoutMs: { type: 'integer', minimum: 1 }
      }
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true }
  },
  {
    name: 'get_task', description: 'Get the current state and result of a relay task',
    inputSchema: { type: 'object', required: ['taskId'], additionalProperties: false, properties: { taskId: { type: 'string', minLength: 1, maxLength: 128 } } },
    annotations: { readOnlyHint: true, openWorldHint: true }
  },
  {
    name: 'cancel_task', description: 'Cancel a queued or running relay task',
    inputSchema: { type: 'object', required: ['taskId'], additionalProperties: false, properties: { taskId: { type: 'string', minLength: 1, maxLength: 128 } } },
    annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: true }
  }
]

function text(value) {
  return JSON.stringify(value)
}

async function request(fetchImpl, baseUrl, path, options) {
  const response = await fetchImpl(`${baseUrl}${path}`, { ...options, signal: AbortSignal.timeout(requestTimeoutMs) })
  let value
  try { value = await response.json() } catch { value = { error: 'invalid_relay_response' } }
  const validObject = value && typeof value === 'object' && !Array.isArray(value)
  if (!response.ok) {
    const data = validObject ? value : { error: 'invalid_relay_response' }
    throw Object.assign(new Error(data.message ?? data.error ?? `Relay returned ${response.status}`), { status: response.status, data })
  }
  if (!validObject) throw new Error('Relay returned a non-object response')
  return value
}

function invalid(message) {
  throw Object.assign(new Error(message), { code: -32602 })
}

function argumentsFor(rpc, allowed) {
  const args = rpc.params?.arguments ?? {}
  if (!args || typeof args !== 'object' || Array.isArray(args)) invalid('arguments must be an object')
  for (const key of Object.keys(args)) if (!allowed.includes(key)) invalid(`Unknown argument: ${key}`)
  return args
}

function nonEmpty(args, key, maxLength) {
  if (typeof args[key] !== 'string' || !args[key].length || (maxLength && args[key].length > maxLength)) invalid(`${key} must be a non-empty string${maxLength ? ` of at most ${maxLength} characters` : ''}`)
}

export function createHttpHandler(baseUrl = relayUrl, fetchImpl = fetch) {
  return async rpc => {
    if (rpc.method === 'initialize') return {
      protocolVersion: '2025-06-18', capabilities: { tools: { listChanged: false } },
      serverInfo: { name: 'a2a-relay-http-mcp', version: '0.1.0' }
    }
    if (rpc.method === 'tools/list') return { tools }
    if (rpc.method === 'ping') return {}
    if (rpc.method !== 'tools/call') throw Object.assign(new Error('Method not found'), { code: -32601 })
    try {
      let value
      switch (rpc.params?.name) {
        case 'list_agents': {
          argumentsFor(rpc, [])
          value = await request(fetchImpl, baseUrl, '/v1/agents')
          break
        }
        case 'delegate': {
          const args = argumentsFor(rpc, ['agentId', 'input', 'sessionId', 'requestId', 'timeoutMs'])
          nonEmpty(args, 'agentId', 128); nonEmpty(args, 'input')
          if (args.sessionId !== undefined) nonEmpty(args, 'sessionId', 128)
          if (args.requestId !== undefined) nonEmpty(args, 'requestId', 128)
          if (args.timeoutMs !== undefined && (!Number.isSafeInteger(args.timeoutMs) || args.timeoutMs <= 0)) invalid('timeoutMs must be a positive integer')
          value = await request(fetchImpl, baseUrl, '/v1/tasks', {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ agentId: args.agentId, input: args.input, ...(args.sessionId !== undefined && { sessionId: args.sessionId }), ...(args.requestId !== undefined && { requestId: args.requestId }), ...(args.timeoutMs !== undefined && { timeoutMs: args.timeoutMs }) })
          })
          break
        }
        case 'get_task': {
          const args = argumentsFor(rpc, ['taskId']); nonEmpty(args, 'taskId', 128)
          value = await request(fetchImpl, baseUrl, `/v1/tasks/${encodeURIComponent(args.taskId)}`)
          break
        }
        case 'cancel_task': {
          const args = argumentsFor(rpc, ['taskId']); nonEmpty(args, 'taskId', 128)
          value = await request(fetchImpl, baseUrl, `/v1/tasks/${encodeURIComponent(args.taskId)}`, { method: 'DELETE' })
          break
        }
        default:
          throw Object.assign(new Error(`Unknown tool: ${rpc.params?.name ?? ''}`), { code: -32602 })
      }
      return { content: [{ type: 'text', text: text(value) }], structuredContent: value }
    } catch (error) {
      if (error.code === -32602) throw error
      const value = { error: error.data?.error ?? 'relay_request_failed', message: error.message, ...(error.status && { status: error.status }) }
      return { content: [{ type: 'text', text: text(value) }], structuredContent: value, isError: true }
    }
  }
}

export async function serve(input = process.stdin, output = process.stdout, handler = createHttpHandler()) {
  const lines = createInterface({ input, crlfDelay: Infinity })
  const pending = new Set()
  for await (const line of lines) {
    if (!line.trim()) continue
    let rpc
    try { rpc = JSON.parse(line) } catch {
      output.write(`${JSON.stringify({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } })}\n`)
      continue
    }
    if (!rpc || typeof rpc !== 'object' || Array.isArray(rpc) || rpc.jsonrpc !== '2.0' || typeof rpc.method !== 'string') {
      output.write(`${JSON.stringify({ jsonrpc: '2.0', id: rpc && typeof rpc === 'object' && !Array.isArray(rpc) && rpc.id !== undefined ? rpc.id : null, error: { code: -32600, message: 'Invalid Request' } })}\n`)
      continue
    }
    if (rpc.id === undefined) continue
    const response = (async () => {
      try {
        output.write(`${JSON.stringify({ jsonrpc: '2.0', id: rpc.id, result: await handler(rpc) })}\n`)
      } catch (error) {
        output.write(`${JSON.stringify({ jsonrpc: '2.0', id: rpc.id, error: { code: error.code ?? -32603, message: error.message } })}\n`)
      }
    })()
    pending.add(response)
    response.finally(() => pending.delete(response))
  }
  await Promise.allSettled([...pending])
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) serve().catch(error => {
  process.stderr.write(`${error.stack ?? error.message}\n`)
  process.exitCode = 1
})
