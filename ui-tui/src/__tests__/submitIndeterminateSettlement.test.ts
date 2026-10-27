import { randomUUID } from 'node:crypto'
import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import { JsonRpcGatewayError } from '@hermes/shared/json-rpc-channel'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { submitPrompt, type SubmitPromptDeps } from '../app/submissionCore.js'
import { captureDestination } from '../app/submissionDestination.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { type QueueItem, useQueue } from '../hooks/useQueue.js'
import { bumpSteerReconcile, loadPendingInputs, savePendingInput } from '../lib/pendingInputs.js'

// A busy correction that could not be confirmed keeps its text and its place, is never replayed on
// its own, and is settled from its receipt after a reconnect, a cold load or a lost reply.

let home: string

beforeEach(() => {
  home = realpathSync(mkdtempSync(join(tmpdir(), 'ink-steer-settle-')))
  vi.stubEnv('HERMES_HOME', home)
  resetUiState()
  patchUiState({
    sid: 'owner',
    info: { model: 'test', profile_name: 'default', skills: {}, stored_session_id: 'S', tools: {} } as never,
    busy: true,
    status: 'running…'
  })
})

afterEach(() => {
  resetUiState()
  vi.unstubAllEnvs()
  rmSync(home, { recursive: true, force: true })
})

const refusal = (reason: string) => new JsonRpcGatewayError(reason, { code: 4001, data: { reason } })

const record = (patch: Partial<QueueItem> = {}): QueueItem => ({
  destination: captureDestination(),
  display: 'correction',
  submissionId: randomUUID(),
  text: 'correction',
  ...patch
})

function deps(gw: unknown): SubmitPromptDeps {
  return { appendMessage: vi.fn(), enqueue: vi.fn(), expand: t => t, gw: gw as never, setLastUserMsg: vi.fn(), sys: vi.fn() }
}

/** Drives the live path with no claim closure: the record is applied straight to the journal. */
async function steerWith(respond: (method: string, params: any) => unknown, item = record()) {
  const calls: Array<{ method: string; params: any }> = []

  const request = vi.fn(async (method: string, params: any) => {
    calls.push({ method, params })

    if (method === 'prompt.submit') {
      return {
        admission_id: 'adm-1', input_id: item.submissionId, status: 'queued',
        target_profile_home: captureDestination().profileHome, target_session_id: 'S'
      }
    }

    return respond(method, params)
  })

  savePendingInput(item)
  submitPrompt(item.text, deps({ isCanonical: true, request }), true, undefined, {
    behindTurn: true, queueItem: item, skipDetectDrop: true, steer: true
  })
  await vi.waitFor(() => expect(calls.some(call => call.method === 'prompt.steer_queued')).toBe(true))
  await new Promise(resolve => setImmediate(resolve))

  return { calls, record: loadPendingInputs(captureDestination())[0] }
}

function rendered(respond: (method: string, params: any) => unknown) {
  const calls: Array<{ method: string; params: any }> = []

  const request = vi.fn(async (method: string, params: any) => {
    calls.push({ method, params })

    return respond(method, params)
  })

  const gw = { request }
  let queue!: ReturnType<typeof useQueue>

  function Harness() {
    queue = useQueue(gw as never)

    return React.createElement(Text, null, String(queue.queuedDisplay.length))
  }

  const instance = renderSync(React.createElement(Harness), {
    stdin: new PassThrough() as never,
    stdout: Object.assign(new PassThrough(), { columns: 80, isRow: false, isTTY: false, rows: 20 }) as never,
    stderr: new PassThrough() as never,
    patchConsole: false
  })

  return { calls, get queue() { return queue }, cleanup() { instance.unmount() } }
}

it('interrupted-steer-surfaces-retryable-with-text', async () => {
  const { record: kept } = await steerWith(() => ({ outcome: 'interrupted', status: 'terminal' }))

  expect(kept).toMatchObject({ failed: true, settlement: 'indeterminate', text: 'correction' })
})

it.each([
  ['unknown', { status: 'unknown' }],
  ['interrupted', { outcome: 'interrupted', status: 'terminal' }],
  ['failed', { outcome: 'failed', status: 'terminal' }],
  ['rejected', { outcome: 'rejected', status: 'terminal' }]
])('admission-started-indeterminate-keeps-text (%s)', async (_name, receipt) => {
  const { calls, record: kept } = await steerWith(method => {
    if (method === 'prompt.steer_queued') { throw refusal('admission_started') }

    return receipt
  })

  expect(calls.map(call => call.method)).toEqual(['prompt.submit', 'prompt.steer_queued', 'prompt.receipt'])
  expect(kept).toMatchObject({ failed: true, settlement: 'indeterminate', text: 'correction' })
})

it.each([
  ['cancelled', { outcome: 'cancelled', status: 'terminal' }, () => ({ outcome: 'cancelled', status: 'terminal' })],
  ['a refused receipt read', undefined, () => { throw refusal('not_found') }]
])('admission-started-cancelled-refused-keeps-text (%s)', async (_name, receipt, respond) => {
  const { record: kept } = await steerWith(method => {
    if (method === 'prompt.steer_queued') { throw refusal('admission_started') }

    return receipt === undefined ? respond() : receipt
  })

  expect(kept).toMatchObject({ failed: true, settlement: 'refused', text: 'correction' })
})

it('steer-refused-not-found-or-denied-resolves-by-receipt', async () => {
  const delivered = await steerWith(method => {
    if (method === 'prompt.steer_queued') { throw refusal('not_found') }

    return { status: 'queued' }
  })

  expect(delivered.calls.map(call => call.method)).toEqual(['prompt.submit', 'prompt.steer_queued', 'prompt.receipt'])
  expect(delivered.record).toBeUndefined()

  const refused = await steerWith(method => {
    if (method === 'prompt.steer_queued') { throw refusal('permission_denied') }

    throw refusal('permission_denied')
  })

  expect(refused.record).toMatchObject({ failed: true, settlement: 'refused', text: 'correction' })
})

it('unknown-admission-not-replayed-keeps-text', async () => {
  const { calls, record: kept } = await steerWith(() => ({ status: 'unknown' }))

  expect(kept).toMatchObject({ failed: true, settlement: 'indeterminate', text: 'correction' })
  await new Promise(resolve => setImmediate(resolve))
  expect(calls.filter(call => call.method === 'prompt.submit')).toHaveLength(1)
  expect(calls.filter(call => call.method === 'prompt.steer_queued')).toHaveLength(1)
})

it('steering-receipt-keeps-record-until-terminal', async () => {
  const inTransfer = record({ admissionId: 'adm-1', controlMethod: 'prompt.steer_queued', steering: true })
  savePendingInput(inTransfer)

  // The gateway's first snapshot already places it in transfer.
  patchUiState({ info: { ...($uiStateInfo()), pending_submissions: [
    { admission_id: 'adm-1', input_id: inTransfer.submissionId, status: 'started', steering: true,
      target_profile_home: home, target_session_id: 'S', user: 'correction' }
  ] } as never })

  const h = rendered(() => ({ outcome: 'interrupted', status: 'terminal' }))

  try {
    // While it is marked steering the record is kept and no receipt is read yet.
    await vi.waitFor(() => expect(h.queue.queueRef.current[0]?.steering).toBe(true))
    expect(h.calls).toEqual([])
    expect(loadPendingInputs(captureDestination())).toHaveLength(1)

    // The admission leaves the pending list: one receipt read decides the record.
    patchUiState({ info: { ...($uiStateInfo()), pending_submissions: [] } as never })
    await vi.waitFor(() => expect(h.calls.filter(call => call.method === 'prompt.receipt')).toHaveLength(1))
    await vi.waitFor(() => expect(h.queue.queueRef.current[0]?.settlement).toBe('indeterminate'))
    expect(loadPendingInputs(captureDestination())[0]).toMatchObject({ settlement: 'indeterminate', text: 'correction' })
  } finally { h.cleanup() }
})

it('reconnect-reconciles-retained-steer-record', async () => {
  savePendingInput(record({ admissionId: 'adm-1', controlMethod: 'prompt.steer_queued' }))

  const h = rendered(() => ({ outcome: 'steered', status: 'terminal' }))

  try {
    bumpSteerReconcile()
    await vi.waitFor(() => expect(h.calls.filter(call => call.method === 'prompt.receipt')).toHaveLength(1))
    await vi.waitFor(() => expect(h.queue.queueRef.current).toHaveLength(0))
    expect(h.calls).toEqual([{ method: 'prompt.receipt', params: { admission_id: 'adm-1', session_id: 'owner' } }])
    expect(loadPendingInputs(captureDestination())).toEqual([])
  } finally { h.cleanup() }
})

it('cold-start-reconciles-retained-steer-record', async () => {
  savePendingInput(record({ admissionId: 'adm-1', controlMethod: 'prompt.steer_queued', createdAt: 1 }))
  // No submit receipt ever named an admission and it is not pending: it cannot have been steered.
  savePendingInput(record({ admissionId: undefined, controlMethod: 'prompt.steer_queued', createdAt: 2, submissionId: randomUUID() }))

  const h = rendered(() => ({ outcome: 'steered', status: 'terminal' }))

  try {
    await vi.waitFor(() => expect(h.calls.filter(call => call.method === 'prompt.receipt')).toHaveLength(1))
    await vi.waitFor(() => expect(h.queue.queueRef.current).toHaveLength(1))
    expect(h.calls.map(call => call.method)).toEqual(['prompt.receipt'])
    expect(h.queue.queueRef.current[0]).toMatchObject({ settlement: 'indeterminate', text: 'correction' })
    expect(loadPendingInputs(captureDestination())).toEqual([
      expect.objectContaining({ settlement: 'indeterminate', text: 'correction' })
    ])
  } finally { h.cleanup() }
})

function $uiStateInfo() {
  return { model: 'test', profile_name: 'default', skills: {}, stored_session_id: 'S', tools: {} }
}
